"""
strategy_selector.py — Auto-selects the best strategy for current market conditions.

Decision logic:
  VIX > 20          → NO TRADE (too volatile)
  VIX 15–20         → TREND_FOLLOW only (high confidence required)
  VIX < 14 + sideways → VWAP_REVERSAL or IRON_CONDOR
  VIX 14–18 + trending → TREND_FOLLOW or BREAKOUT
  Pre-event day       → STRADDLE
  Expiry morning      → SCALP (fast in/out before theta kills)
  High volume open    → SCALP or BREAKOUT
"""

from datetime import datetime, time as dtime, date
from typing import Optional
from loguru import logger
import pandas as pd

import config
from strategies import ALL_STRATEGIES, StrategySignal
from signals_advanced import OptionChainData


# ── Market Regime Detection ───────────────────────────────────

def detect_market_regime(
    df: pd.DataFrame,
    vix: float,
    option_chain: Optional[OptionChainData] = None,
) -> str:
    """
    Classify current market condition into one of:
      TRENDING_UP   — clear uptrend
      TRENDING_DOWN — clear downtrend
      SIDEWAYS      — low ADX, rangebound
      HIGH_VOLATILITY — VIX spike
      PRE_EVENT     — within 1 day of major event

    Used to filter which strategies are appropriate.
    """
    if vix > config.VIX_MAX_THRESHOLD:
        return "HIGH_VOLATILITY"

    if len(df) < 20:
        return "UNKNOWN"

    # ADX approximation using EMA slope
    from strategy import compute_ema
    df = compute_ema(df.copy())
    df = df.dropna()

    if len(df) < 5:
        return "UNKNOWN"

    ema_slope = df["ema_fast"].iloc[-1] - df["ema_fast"].iloc[-5]
    close_5   = df["close"].iloc[-5]

    # Trend strength: if EMA moved more than 0.3% of price in 5 candles → trending
    threshold = close_5 * 0.003

    if ema_slope > threshold:
        return "TRENDING_UP"
    elif ema_slope < -threshold:
        return "TRENDING_DOWN"
    else:
        return "SIDEWAYS"


def select_strategy(
    df_5min: pd.DataFrame,
    df_1min: Optional[pd.DataFrame],
    vix: float,
    option_chain: Optional[OptionChainData] = None,
    spot: float = 0.0,
) -> tuple[Optional[StrategySignal], str]:
    """
    Auto-selects and runs the best strategy for current conditions.

    Returns: (StrategySignal | None, regime_name)
    """
    now   = datetime.now().time()
    today = date.today()
    dow   = today.weekday()  # 0=Mon, 6=Sun

    # ── Hard blocks ───────────────────────────────────────────
    if vix > config.VIX_MAX_THRESHOLD:
        logger.info(f"🚫 VIX={vix:.1f} — all strategies blocked")
        return None, "HIGH_VOLATILITY"

    if dow >= 5:
        return None, "WEEKEND"

    # ── Detect regime ─────────────────────────────────────────
    regime = detect_market_regime(df_5min, vix, option_chain)
    iv_rank = option_chain.iv_rank if option_chain else 50
    is_thursday = dow == 3
    is_expiry_afternoon = is_thursday and now >= dtime(13, 0)

    logger.info(f"🌡️  Market regime: {regime} | VIX={vix:.1f} | IVR={iv_rank:.0f}")

    # ── Strategy selection by regime + time ───────────────────

    # 1. SCALP — expiry morning or high-VIX trending open
    if is_thursday and dtime(9, 20) <= now <= dtime(12, 30):
        if df_1min is not None and len(df_1min) >= 10:
            sig = ALL_STRATEGIES["SCALP"].should_enter(df_1min, vix)
            if sig:
                logger.info(f"⚡ Strategy: SCALP (expiry day)")
                return sig, regime

    # 2. IRON CONDOR — sideways mid-week with fat premiums
    if regime == "SIDEWAYS" and vix < 14 and iv_rank > 50:
        sig = ALL_STRATEGIES["IRON_CONDOR"].should_enter(vix, iv_rank, spot)
        if sig:
            logger.info("🦅 Strategy: IRON_CONDOR")
            return sig, regime

    # 3. STRADDLE — near major events
    # (event detection is manual — set EVENT_IN_DAYS in .env)
    import os
    event_in_days = int(os.getenv("EVENT_IN_DAYS", "99"))
    if event_in_days <= 1 and not is_expiry_afternoon:
        sig = ALL_STRATEGIES["STRADDLE"].should_enter(vix, iv_rank, event_in_days)
        if sig:
            logger.info("⚡ Strategy: STRADDLE (pre-event)")
            return sig, regime

    # 4. VWAP REVERSAL — sideways market, mid-session
    if regime == "SIDEWAYS" and now >= dtime(10, 30):
        sig = ALL_STRATEGIES["VWAP_REVERSAL"].should_enter(df_5min, vix)
        if sig:
            logger.info("↩️  Strategy: VWAP_REVERSAL")
            return sig, regime

    # 5. BREAKOUT — after opening range forms
    if now >= dtime(9, 45) and regime in ("SIDEWAYS", "TRENDING_UP", "TRENDING_DOWN"):
        sig = ALL_STRATEGIES["BREAKOUT"].should_enter(df_5min, vix)
        if sig:
            logger.info("💥 Strategy: BREAKOUT")
            return sig, regime

    # 6. TREND FOLLOW — default for trending markets
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        sig = ALL_STRATEGIES["TREND_FOLLOW"].should_enter(df_5min, vix)
        if sig:
            logger.info("📈 Strategy: TREND_FOLLOW")
            return sig, regime

    logger.debug(f"No strategy triggered | regime={regime}")
    return None, regime
