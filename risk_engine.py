"""
risk_engine.py — Professional risk management for live trading.

Covers:
  - Trailing stop-loss (moves SL up as trade profits)
  - Dynamic position sizing (Kelly Criterion / fixed fractional)
  - Event calendar (no trading on Budget, RBI, expiry day special rules)
  - Daily/weekly drawdown circuit breakers
  - Max consecutive loss protection
"""

import math
from datetime import date, datetime, time as dtime
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

import config


# ── Known market-moving event dates (update annually) ─────────
# Format: "YYYY-MM-DD"
BLACKOUT_DATES: set[str] = {
    # RBI Monetary Policy Committee dates (6 per year)
    "2026-04-09", "2026-06-06", "2026-08-08", "2026-10-08", "2026-12-06",
    "2027-02-07",
    # Union Budget
    "2027-02-01",
    # NSE Holidays 2026 (markets closed)
    "2026-01-26", "2026-03-25", "2026-04-14", "2026-04-17",
    "2026-05-01", "2026-08-15", "2026-10-02", "2026-10-20",
    "2026-10-21", "2026-11-05", "2026-12-25",
}

# On these dates, trade with REDUCED size (50%) but don't blackout
CAUTION_DATES: set[str] = {
    # Day before/after major events — add as known
    "2026-12-24",   # Christmas Eve
}


@dataclass
class RiskState:
    """Mutable state for one trading day's risk tracking."""
    date:               str   = field(default_factory=lambda: date.today().isoformat())
    daily_pnl:          float = 0.0
    trade_count:        int   = 0
    consecutive_losses: int   = 0
    consecutive_wins:   int   = 0
    peak_pnl:           float = 0.0     # highest daily P&L reached today
    lots_traded:        int   = 0

    def reset_for_new_day(self):
        today = date.today().isoformat()
        if self.date != today:
            logger.info(f"📅 Risk state reset for {today}")
            self.date               = today
            self.daily_pnl          = 0.0
            self.trade_count        = 0
            self.consecutive_losses = 0
            self.consecutive_wins   = 0
            self.peak_pnl           = 0.0
            self.lots_traded        = 0


class RiskEngine:
    """
    Central risk manager.

    Usage:
        re = RiskEngine()
        if re.can_trade():
            lots = re.position_size(win_rate=0.55, avg_win=20, avg_loss=10)
            ...
        re.record_trade(pnl_points=15)
    """

    def __init__(self):
        self.state = RiskState()

    # ── Calendar checks ───────────────────────────────────────

    def is_blackout_day(self) -> tuple[bool, str]:
        """Returns (True, reason) if trading should be completely skipped today."""
        today = date.today().isoformat()
        dow   = date.today().weekday()  # 0=Mon, 6=Sun

        if dow >= 5:
            return True, "Weekend — NSE closed"

        if today in BLACKOUT_DATES:
            return True, f"Blackout date: {today} (holiday/major event)"

        return False, ""

    def is_caution_day(self) -> bool:
        """Returns True on reduced-size trading days."""
        return date.today().isoformat() in CAUTION_DATES

    def is_expiry_day(self) -> bool:
        """True if today is Thursday (weekly F&O expiry)."""
        return date.today().weekday() == 3  # Thursday

    def is_expiry_afternoon(self) -> bool:
        """After 1 PM on expiry day — theta decay kills option buyers."""
        return self.is_expiry_day() and datetime.now().time() >= dtime(13, 0)

    # ── Trade permission ──────────────────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        """
        Master gate — returns (True, "") if a new trade is allowed.
        Returns (False, reason) if trading should be blocked.
        """
        self.state.reset_for_new_day()

        # 1. Blackout date
        blocked, reason = self.is_blackout_day()
        if blocked:
            return False, reason

        # 2. Daily profit target hit
        if self.state.daily_pnl >= config.DAILY_PROFIT_TARGET:
            return False, f"Daily target ₹{config.DAILY_PROFIT_TARGET:.0f} reached ✅"

        # 3. Daily max loss hit
        if self.state.daily_pnl <= -config.DAILY_MAX_LOSS:
            return False, f"Daily loss limit ₹{config.DAILY_MAX_LOSS:.0f} hit 🛑"

        # 4. Max trades per day
        if self.state.trade_count >= config.MAX_TRADES_PER_DAY:
            return False, f"Max {config.MAX_TRADES_PER_DAY} trades/day reached"

        # 5. Consecutive loss protection (3 losses in a row → stop)
        max_consec = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
        if self.state.consecutive_losses >= max_consec:
            return False, f"{self.state.consecutive_losses} consecutive losses — cooling off"

        # 6. Expiry afternoon — option buyers get crushed by theta
        if self.is_expiry_afternoon():
            return False, "Expiry afternoon — theta risk too high for buyers"

        return True, ""

    # ── Position sizing ───────────────────────────────────────

    def position_size(
        self,
        win_rate: float = 0.5,
        avg_win_pts: float = None,
        avg_loss_pts: float = None,
        method: str = "fixed_fractional",
    ) -> int:
        """
        Calculate number of lots to trade.

        Methods:
          fixed_fractional : Always 1 lot (safe default)
          kelly            : Kelly Criterion — optimal fraction of capital
          half_kelly       : Half-Kelly (conservative, recommended)

        Returns: number of lots (minimum 1, maximum MAX_LOTS_PER_TRADE)
        """
        import os
        max_lots = int(os.getenv("MAX_LOTS_PER_TRADE", "1"))

        # Caution day → halve position
        if self.is_caution_day():
            logger.info("⚠️ Caution day — reducing to 1 lot")
            return 1

        if method == "fixed_fractional" or win_rate <= 0:
            return 1

        avg_win  = avg_win_pts  or config.PROFIT_TARGET_POINTS
        avg_loss = avg_loss_pts or config.STOP_LOSS_POINTS

        # Kelly Criterion: f* = (p*b - q) / b
        # where p=win_rate, q=1-p, b=win/loss ratio
        b = avg_win / avg_loss if avg_loss > 0 else 1
        q = 1 - win_rate
        kelly = (win_rate * b - q) / b

        if kelly <= 0:
            return 1  # edge-less strategy → minimum size

        fraction = kelly / 2 if method == "half_kelly" else kelly
        lots = max(1, min(int(fraction * 4), max_lots))  # scale to max 4 lots
        logger.debug(f"Position size: {lots} lots (Kelly={kelly:.2f}, method={method})")
        return lots

    # ── Trailing stop-loss ────────────────────────────────────

    def trailing_stop(
        self,
        entry_price: float,
        current_ltp: float,
        highest_ltp: float,
        trail_pts: float = None,
    ) -> tuple[bool, str]:
        """
        Trailing stop-loss logic.

        Trail rule:
          - Once profit >= TRAIL_ACTIVATION_POINTS, activate trailing
          - SL trails TRAIL_DISTANCE_POINTS below the highest LTP seen

        Returns: (should_exit, reason)
        """
        trail_activation = float(os.getenv("TRAIL_ACTIVATION_POINTS", "12"))
        trail_distance   = float(os.getenv("TRAIL_DISTANCE_POINTS",   "8"))
        trail_pts        = trail_pts or trail_distance

        pnl = current_ltp - entry_price

        # Hard stop-loss (never removed)
        if pnl <= -config.STOP_LOSS_POINTS:
            return True, f"HARD_STOPLOSS {pnl:+.1f}pts"

        # Target hit
        if pnl >= config.PROFIT_TARGET_POINTS:
            return True, f"TARGET {pnl:+.1f}pts"

        # Trailing SL (only once we've reached activation threshold)
        if highest_ltp - entry_price >= trail_activation:
            trail_sl = highest_ltp - trail_pts
            if current_ltp <= trail_sl:
                return True, (
                    f"TRAILING_SL {pnl:+.1f}pts "
                    f"(peak={highest_ltp-entry_price:+.1f}, trail={trail_pts})"
                )

        return False, ""

    # ── Trade recording ───────────────────────────────────────

    def record_trade(self, pnl_points: float):
        """Update risk state after a trade closes."""
        self.state.reset_for_new_day()
        pnl_inr = pnl_points * config.LOT_SIZE
        self.state.daily_pnl    += pnl_inr
        self.state.trade_count  += 1
        self.state.lots_traded  += 1
        self.state.peak_pnl      = max(self.state.peak_pnl, self.state.daily_pnl)

        if pnl_points > 0:
            self.state.consecutive_losses = 0
            self.state.consecutive_wins  += 1
        else:
            self.state.consecutive_wins   = 0
            self.state.consecutive_losses += 1

        logger.info(
            f"📊 Risk state | Daily P&L: ₹{self.state.daily_pnl:+.0f} | "
            f"Trades: {self.state.trade_count} | "
            f"Consec L/W: {self.state.consecutive_losses}/{self.state.consecutive_wins}"
        )

    @property
    def daily_pnl(self) -> float:
        self.state.reset_for_new_day()
        return self.state.daily_pnl


import os  # needed for os.getenv calls above
