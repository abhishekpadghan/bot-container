"""
strategies.py — All trading strategies for NiftyBot.

Strategies implemented:
  1. TREND_FOLLOW    — Supertrend + EMA + RSI + VWAP (existing, upgraded)
  2. SCALP           — 1-min fast entries, tight SL, quick exits
  3. STRADDLE        — Buy CE + PE simultaneously on high-volatility events
  4. VWAP_REVERSAL   — Mean reversion when price deviates far from VWAP
  5. BREAKOUT        — Buy after price breaks consolidation range
  6. IRON_CONDOR     — Sell OTM CE + PE (neutral strategy, sideways market)

Each strategy exposes:
  - should_enter(df, context) → StrategySignal | None
  - should_exit(trade, ltp, df) → (bool, reason)
  - name, description, best_conditions
"""

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

import config
from strategy import (
    compute_supertrend, compute_ema, compute_rsi, compute_vwap, Signal, OptionType
)


@dataclass
class StrategySignal:
    strategy:     str
    signal:       Signal
    option_type:  Optional[OptionType]
    confidence:   int          # 0–10
    reason:       str
    suggested_sl_pts:  float = None   # override default SL   (option PREMIUM points)
    suggested_tgt_pts: float = None   # override default target (None = no fixed target, trail only)
    trail_at_pts:      float = None   # start trailing once this much profit is reached (premium pts)
    trail_dist_pts:    float = None   # trail this far behind the peak (premium pts)


# ─────────────────────────────────────────────────────────────
# 1. TREND FOLLOWING (upgraded version — same logic, better scoring)
# ─────────────────────────────────────────────────────────────

class TrendFollowStrategy:
    name = "TREND_FOLLOW"
    description = "Supertrend + EMA + RSI + VWAP confluence"
    best_conditions = "Trending market, VIX 12–20, mid-morning 9:30–11:30"

    def should_enter(self, df: pd.DataFrame, vix: float = 0) -> Optional[StrategySignal]:
        if vix > config.VIX_MAX_THRESHOLD:
            return None

        df = compute_supertrend(df)
        df = compute_ema(df)
        df = compute_rsi(df)
        df = compute_vwap(df)
        df = df.dropna()

        if len(df) < 25:
            return None

        # df holds COMPLETED candles only (see main._closed_candles) → last row is the signal bar
        row = df.iloc[-1]
        bull = (
            (row["supertrend_dir"] == "UP")
            + (row["ema_fast"] > row["ema_slow"])
            + (45 <= row["rsi"] <= 65)
            + (row["close"] > row["vwap"])
        )
        # The original code only scored the bullish side, so PE entries were unreachable.
        bear = (
            (row["supertrend_dir"] == "DOWN")
            + (row["ema_fast"] < row["ema_slow"])
            + (35 <= row["rsi"] <= 55)
            + (row["close"] < row["vwap"])
        )

        if bull >= 3:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.CE, confidence=int(bull) * 2,
                reason=f"Trend Bull score={int(bull)}/4",
                suggested_tgt_pts=config.PROFIT_TARGET_POINTS,
                suggested_sl_pts=config.STOP_LOSS_POINTS,
            )
        if bear >= 3:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.PE, confidence=int(bear) * 2,
                reason=f"Trend Bear score={int(bear)}/4",
                suggested_tgt_pts=config.PROFIT_TARGET_POINTS,
                suggested_sl_pts=config.STOP_LOSS_POINTS,
            )
        return None


# ─────────────────────────────────────────────────────────────
# 2. SCALPING (1-min candles, tight targets)
# ─────────────────────────────────────────────────────────────

class ScalpStrategy:
    name = "SCALP"
    description = "Fast 1-min entries on momentum bursts"
    best_conditions = "High volume, trending open (9:15–10:30), VIX > 15"

    # Scalp targets are smaller and faster
    SCALP_TARGET = 8    # pts
    SCALP_SL     = 5    # pts

    def should_enter(self, df_1min: pd.DataFrame, vix: float = 0) -> Optional[StrategySignal]:
        """Requires 1-minute candle data."""
        if len(df_1min) < 10:
            return None

        df = compute_ema(df_1min.copy())
        df = compute_rsi(df)
        df = df.dropna()

        if len(df) < 10:
            return None

        row  = df.iloc[-1]
        prev = df.iloc[-2]

        # Momentum: fast EMA just crossed above slow EMA + RSI accelerating
        bullish_cross = (prev["ema_fast"] <= prev["ema_slow"] and
                         row["ema_fast"] > row["ema_slow"])
        bearish_cross = (prev["ema_fast"] >= prev["ema_slow"] and
                         row["ema_fast"] < row["ema_slow"])

        # Volume surge (if volume data available)
        vol_surge = True
        if "volume" in df.columns and df["volume"].iloc[-5:].mean() > 0:
            avg_vol = df["volume"].iloc[-20:-5].mean()
            vol_surge = row["volume"] > avg_vol * 1.5

        if bullish_cross and 50 <= row["rsi"] <= 70 and vol_surge:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.CE, confidence=7,
                reason="Scalp Bull: EMA cross + RSI + volume",
                suggested_tgt_pts=self.SCALP_TARGET,
                suggested_sl_pts=self.SCALP_SL,
            )
        if bearish_cross and 30 <= row["rsi"] <= 50 and vol_surge:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.PE, confidence=7,
                reason="Scalp Bear: EMA cross + RSI + volume",
                suggested_tgt_pts=self.SCALP_TARGET,
                suggested_sl_pts=self.SCALP_SL,
            )
        return None


# ─────────────────────────────────────────────────────────────
# 3. STRADDLE (buy CE + PE together — profits from big moves)
# ─────────────────────────────────────────────────────────────

class StraddleStrategy:
    name = "STRADDLE"
    description = "Buy ATM CE + PE — profits if market moves sharply either way"
    best_conditions = "Pre-event (RBI/Budget/Results), VIX rising, IV < 18"

    # Straddle-specific targets
    STRADDLE_TARGET_PCT = 0.30   # exit when combined premium up 30%
    STRADDLE_SL_PCT     = 0.20   # stop if combined premium down 20%

    def should_enter(
        self, vix: float, iv_rank: float = 50, time_to_event_days: int = 99
    ) -> Optional[StrategySignal]:
        """
        Enter straddle when:
          - VIX is elevated (>14) but IV Rank is still low (<40) — premiums cheap
          - Major event within 2 days
          - NOT on expiry day (theta destroys both legs)
        """
        from datetime import date
        is_thursday = date.today().weekday() == 3

        if is_thursday:
            return None  # Too much theta decay on expiry day

        if vix > 14 and iv_rank < 40 and time_to_event_days <= 2:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=None,  # Both CE and PE
                confidence=8,
                reason=f"Straddle: VIX={vix:.1f}, IVR={iv_rank:.0f}, event in {time_to_event_days}d",
                suggested_tgt_pts=None,  # managed by % change on premium
                suggested_sl_pts=None,
            )
        return None


# ─────────────────────────────────────────────────────────────
# 4. VWAP REVERSAL (mean reversion when price deviates from VWAP)
# ─────────────────────────────────────────────────────────────

class VWAPReversalStrategy:
    name = "VWAP_REVERSAL"
    description = "Mean reversion when price deviates >0.5% from VWAP"
    best_conditions = "Sideways/choppy market, VIX < 14, post 10:30 AM"

    DEVIATION_PCT = 0.5   # % deviation from VWAP to trigger signal

    def should_enter(self, df: pd.DataFrame, vix: float = 0) -> Optional[StrategySignal]:
        if vix > 18:   # VWAP reversal doesn't work in trending/volatile markets
            return None

        df = compute_vwap(df.copy())
        df = compute_rsi(df)
        df = df.dropna()

        if len(df) < 10:
            return None

        row = df.iloc[-1]   # df holds completed candles only
        close, vwap, rsi = row["close"], row["vwap"], row["rsi"]

        if vwap == 0:
            return None

        deviation_pct = (close - vwap) / vwap * 100

        # Price significantly below VWAP + RSI oversold → expect bounce UP → buy CE
        if deviation_pct < -self.DEVIATION_PCT and rsi < 40:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.CE, confidence=6,
                reason=f"VWAP Reversal Bull: dev={deviation_pct:.1f}% RSI={rsi:.0f}",
                suggested_tgt_pts=15,
                suggested_sl_pts=8,
            )

        # Price significantly above VWAP + RSI overbought → expect pullback → buy PE
        if deviation_pct > self.DEVIATION_PCT and rsi > 60:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.PE, confidence=6,
                reason=f"VWAP Reversal Bear: dev={deviation_pct:.1f}% RSI={rsi:.0f}",
                suggested_tgt_pts=15,
                suggested_sl_pts=8,
            )

        return None


# ─────────────────────────────────────────────────────────────
# 5. BREAKOUT STRATEGY
# ─────────────────────────────────────────────────────────────

class BreakoutStrategy:
    name = "BREAKOUT"
    description = "Trade breakout from N-candle consolidation range"
    best_conditions = "Post-opening range established (after 9:45 AM), low VIX"

    LOOKBACK   = 10    # candles to define range
    MIN_RANGE  = 30    # minimum range in points (filters noise)

    def should_enter(self, df: pd.DataFrame, vix: float = 0) -> Optional[StrategySignal]:
        from datetime import datetime, time as dtime
        if datetime.now().time() < dtime(9, 45):
            return None   # wait for opening range to form

        if len(df) < self.LOOKBACK + 2:
            return None

        recent = df.iloc[-(self.LOOKBACK + 1):-1]
        range_high = recent["high"].max()
        range_low  = recent["low"].min()
        range_size = range_high - range_low

        if range_size < self.MIN_RANGE:
            return None   # range too tight — noise, not signal

        current_close = df.iloc[-1]["close"]

        # Breakout UP: close above range high
        if current_close > range_high:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.CE, confidence=7,
                reason=f"Breakout UP: close={current_close:.0f} > high={range_high:.0f}",
                suggested_tgt_pts=range_size * 0.5,
                suggested_sl_pts=range_size * 0.3,
            )

        # Breakout DOWN: close below range low
        if current_close < range_low:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.PE, confidence=7,
                reason=f"Breakout DOWN: close={current_close:.0f} < low={range_low:.0f}",
                suggested_tgt_pts=range_size * 0.5,
                suggested_sl_pts=range_size * 0.3,
            )

        return None


# ─────────────────────────────────────────────────────────────
# 6. IRON CONDOR (sell OTM options — neutral/sideways market)
# ─────────────────────────────────────────────────────────────

class IronCondorStrategy:
    name = "IRON_CONDOR"
    description = "Sell OTM CE + OTM PE — profits when Nifty stays rangebound"
    best_conditions = "VIX < 14, sideways market, mid-week (Tue-Wed), IV Rank > 50"

    # Wings: how far OTM to sell (in strike multiples of 50)
    SELL_DISTANCE = 100  # sell 100 pts OTM
    BUY_DISTANCE  = 200  # buy protection 200 pts OTM (defines max loss)

    def should_enter(
        self, vix: float, iv_rank: float = 50, spot: float = 0
    ) -> Optional[StrategySignal]:
        from datetime import date
        dow = date.today().weekday()  # 0=Mon, 6=Sun

        # Iron Condor works best mid-week when there's enough time decay
        if dow not in (1, 2):  # Only Tuesday and Wednesday
            return None

        # Needs high IV (so premiums are fat to sell) and low VIX (rangebound)
        if vix > 14 or iv_rank < 50:
            return None

        return StrategySignal(
            strategy=self.name, signal=Signal.BUY,
            option_type=None,  # sells both CE and PE legs
            confidence=7,
            reason=f"Iron Condor: VIX={vix:.1f}, IVR={iv_rank:.0f}, spot={spot:.0f}",
            suggested_tgt_pts=None,   # managed by premium collected
            suggested_sl_pts=None,
        )


# ─────────────────────────────────────────────────────────────
# 7. TREND DAY (selective continuation — the bot's default strategy)
# ─────────────────────────────────────────────────────────────

class TrendDayStrategy:
    """
    Trades only clear trend days, at most once per day:
      * price is >= MIN_MOVE away from the day's open,
      * on the right side of the session average price and the 9/21 EMAs,
      * and the just-closed 5-min candle prints a fresh session high (CE) / low (PE).
    Risk is ATR-scaled and trail-only (no fixed target) so winners can run.

    On 70 days of real Nifty candles (research/strategy_lab.py) this had the smallest
    net loss of everything tested and trades rarely (so costs bite less) — but it was
    still NOT profitable after costs. Treat it as a paper-trading experiment.
    """
    name = "TREND_DAY"
    description = "Fresh session high/low on a >0.4% trend day, ATR stop, trail-only exit"
    best_conditions = "Strong directional days; deliberately stays flat on choppy days"

    MIN_MOVE      = float(os.getenv("TREND_DAY_MIN_MOVE", "0.004"))   # 0.4% from the open
    FIRST_BAR     = 12      # nothing before the 12th 5-min bar (10:15) — let the day establish itself
    SL_ATR        = 1.0     # stop distance in ATRs of the index
    TRAIL_AT_R    = 1.0     # start trailing after +1R
    TRAIL_DIST_R  = 0.5     # trail 0.5R behind the peak
    OPTION_DELTA  = 0.5     # ATM option: premium moves ~0.5 pt per index pt
    MIN_SL_PREM   = 4.0     # never tighter than this (premium points)

    def __init__(self) -> None:
        self._fired: dict[str, date] = {}      # instrument → day it already traded

    def on_entry(self, instrument: str = "") -> None:
        """Called by the bot after a trade is actually opened — one trade per day per instrument."""
        self._fired[instrument] = date.today()

    def should_enter(
        self, df: pd.DataFrame, vix: float = 0,
        instrument: str = "", lot_size: Optional[int] = None, cost: Optional[float] = None,
    ) -> Optional[StrategySignal]:
        if vix > config.VIX_MAX_THRESHOLD or self._fired.get(instrument) == date.today():
            return None
        if len(df) < 30:
            return None

        df = df.copy()
        df["_d"] = pd.to_datetime(df["datetime"]).dt.date
        day = df[df["_d"] == df["_d"].iloc[-1]]
        if len(day) - 1 < self.FIRST_BAR:
            return None

        df = compute_ema(df)
        pc = df["close"].shift()
        tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])

        last        = df.iloc[-1]
        close       = float(last["close"])
        day_open    = float(day["open"].iloc[0])
        move        = (close - day_open) / day_open
        typical     = (day["high"] + day["low"] + day["close"]) / 3
        session_avg = float(typical.mean())          # index candles have no volume → no true VWAP
        sess_hi     = float(day["high"].iloc[:-1].max())
        sess_lo     = float(day["low"].iloc[:-1].min())
        bull_ema    = last["ema_fast"] > last["ema_slow"]

        if move >= self.MIN_MOVE and close > session_avg and bull_ema and close > sess_hi:
            opt, why = OptionType.CE, f"up {move*100:.2f}% from open, new session high {close:.0f}>{sess_hi:.0f}"
        elif move <= -self.MIN_MOVE and close < session_avg and not bull_ema and close < sess_lo:
            opt, why = OptionType.PE, f"down {abs(move)*100:.2f}% from open, new session low {close:.0f}<{sess_lo:.0f}"
        else:
            return None

        # Stop in option-premium points, capped so one stopped-out trade (stop + costs)
        # cannot exceed MAX_LOSS_PER_TRADE. Lot size / costs / index level differ per
        # instrument, so the cap and the minimum stop distance are scaled accordingly.
        lot      = lot_size or config.LOT_SIZE
        cost     = config.COST_PER_TRADE if cost is None else cost
        cap_prem = (config.MAX_LOSS_PER_TRADE - cost) / lot
        floor_idx = 0.00035 * close                      # ≈ 8 pts on Nifty, ≈ 25 on Sensex
        sl_prem = max(self.MIN_SL_PREM, min(max(self.SL_ATR * atr, floor_idx) * self.OPTION_DELTA, cap_prem))
        return StrategySignal(
            strategy=self.name, signal=Signal.BUY, option_type=opt, confidence=7,
            reason=f"Trend day: {why}",
            suggested_sl_pts=round(sl_prem, 1),
            suggested_tgt_pts=None,
            trail_at_pts=round(sl_prem * self.TRAIL_AT_R, 1),
            trail_dist_pts=round(sl_prem * self.TRAIL_DIST_R, 1),
        )


# ─────────────────────────────────────────────────────────────
# Strategy Registry — all strategies in one place
# ─────────────────────────────────────────────────────────────

ALL_STRATEGIES = {
    "TREND_DAY":      TrendDayStrategy(),
    "TREND_FOLLOW":   TrendFollowStrategy(),
    "SCALP":          ScalpStrategy(),
    "STRADDLE":       StraddleStrategy(),
    "VWAP_REVERSAL":  VWAPReversalStrategy(),
    "BREAKOUT":       BreakoutStrategy(),
    "IRON_CONDOR":    IronCondorStrategy(),
}

# Only these run live. Everything else lost money on 70 days of real data (see
# research/strategy_lab.py); add names here via ENABLED_STRATEGIES to experiment.
# IRON_CONDOR is excluded from execution: it needs a multi-leg SELL order, which the
# single-leg paper/live trader cannot place (it would have bought a plain CE instead).
ENABLED_STRATEGIES = {
    s.strip().upper()
    for s in os.getenv("ENABLED_STRATEGIES", "TREND_DAY").split(",") if s.strip()
} - {"IRON_CONDOR"}
