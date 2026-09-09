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

from dataclasses import dataclass
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
    suggested_sl_pts:  float = None   # override default SL
    suggested_tgt_pts: float = None   # override default target


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

        row = df.iloc[-2]
        score = 0
        if row["supertrend_dir"] == "UP":     score += 1
        if row["ema_fast"] > row["ema_slow"]: score += 1
        if 45 <= row["rsi"] <= 65:            score += 1
        if row["close"] > row["vwap"]:        score += 1

        if score >= 3:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.CE, confidence=score * 2,
                reason=f"Trend Bull score={score}/4",
                suggested_tgt_pts=config.PROFIT_TARGET_POINTS,
                suggested_sl_pts=config.STOP_LOSS_POINTS,
            )
        if score <= -3:
            return StrategySignal(
                strategy=self.name, signal=Signal.BUY,
                option_type=OptionType.PE, confidence=abs(score) * 2,
                reason=f"Trend Bear score={score}/4",
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

        row = df.iloc[-2]
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
# Strategy Registry — all strategies in one place
# ─────────────────────────────────────────────────────────────

ALL_STRATEGIES = {
    "TREND_FOLLOW":   TrendFollowStrategy(),
    "SCALP":          ScalpStrategy(),
    "STRADDLE":       StraddleStrategy(),
    "VWAP_REVERSAL":  VWAPReversalStrategy(),
    "BREAKOUT":       BreakoutStrategy(),
    "IRON_CONDOR":    IronCondorStrategy(),
}
