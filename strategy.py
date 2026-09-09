"""
strategy.py — Signal Engine for NiftyBot

═══════════════════════════════════════════════════════════════════
ENTRY / EXIT DECISION ALGORITHM
═══════════════════════════════════════════════════════════════════

ENTRY CONDITIONS (ALL must be true simultaneously):
────────────────────────────────────────────────────
  1. SUPERTREND  → Direction must be BUY (price above Supertrend line)
  2. EMA         → Fast EMA (9) crosses above Slow EMA (21)  [bullish crossover]
  3. RSI         → RSI(14) is between 45–65 (not overbought, not oversold)
                   Avoid RSI > 70 (overbought — premium already pumped)
                   Avoid RSI < 30 (oversold — trend may be reversing down)
  4. VWAP        → Current candle close > VWAP (price above fair value → bullish)
  5. VIX FILTER  → India VIX < threshold (default 20) — skip if too volatile
  6. TIME WINDOW → Only between ENTRY_TIME_START and ENTRY_TIME_END (e.g. 9:20–14:30)
  7. DAILY LIMIT → Daily P&L has not hit DAILY_PROFIT_TARGET or DAILY_MAX_LOSS

For CE (Call) buy:  All above conditions confirm BULLISH bias
For PE (Put)  buy:  All conditions inverted (Supertrend DOWN, EMA bearish, price < VWAP)

EXIT CONDITIONS (First one triggered wins):
────────────────────────────────────────────
  1. TARGET HIT  → Premium gained +PROFIT_TARGET_POINTS (default +20 pts)
  2. STOP-LOSS   → Premium lost -STOP_LOSS_POINTS (default -10 pts)
  3. SIGNAL FLIP → Supertrend direction reverses (early exit, protects profit)
  4. TIME EXIT   → EXIT_ALL_TIME reached (default 15:15 IST) — force close
  5. DAILY LIMIT → Bot's daily P&L target or loss limit reached

RISK : REWARD = 1 : 2  (10 pts risk → 20 pts reward)

═══════════════════════════════════════════════════════════════════
INDICATOR CALCULATIONS
═══════════════════════════════════════════════════════════════════

  SUPERTREND(7, 3):
    - ATR (Average True Range) over 7 periods
    - Upper Band = (High+Low)/2 + 3 × ATR
    - Lower Band = (High+Low)/2 - 3 × ATR
    - If close > Upper Band → SELL signal (trend is down)
    - If close < Lower Band → BUY  signal (trend is up)

  EMA CROSSOVER (9/21):
    - EMA_9  = 9-period Exponential Moving Average of close
    - EMA_21 = 21-period Exponential Moving Average of close
    - BUY  signal when EMA_9 crosses above EMA_21
    - SELL signal when EMA_9 crosses below EMA_21

  RSI (14):
    - Relative Strength Index over 14 periods
    - Range 0–100
    - > 70: Overbought (avoid buying)
    - < 30: Oversold   (avoid buying puts as reversal may come)
    - 45–65: Sweet spot for entry confirmation

  VWAP:
    - Volume Weighted Average Price (intraday, resets each day)
    - Price above VWAP = bullish bias → buy CE
    - Price below VWAP = bearish bias → buy PE

═══════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

import config


class Signal(Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OptionType(Enum):
    CE = "CE"   # Call — profit when Nifty goes UP
    PE = "PE"   # Put  — profit when Nifty goes DOWN


@dataclass
class StrategyResult:
    signal:       Signal
    option_type:  Optional[OptionType]
    confidence:   int          # 0–5 (number of indicators agreeing)
    supertrend:   str          # "UP" | "DOWN"
    rsi:          float
    ema_fast:     float
    ema_slow:     float
    vwap:         float
    close:        float
    reason:       str          # Human-readable explanation of decision


# ── Supertrend ────────────────────────────────────────────────

def compute_supertrend(
    df: pd.DataFrame,
    period: int = None,
    multiplier: float = None
) -> pd.DataFrame:
    """
    Compute Supertrend indicator.

    Adds columns to df:
      - supertrend_val  : the Supertrend line value
      - supertrend_dir  : "UP" (buy) or "DOWN" (sell)
    """
    period     = period     or config.SUPERTREND_PERIOD
    multiplier = multiplier or config.SUPERTREND_MULTIPLIER

    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # True Range
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()

    hl2 = (high + low) / 2
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    supertrend = pd.Series(index=df.index, dtype=float)
    direction  = pd.Series(index=df.index, dtype=str)

    for i in range(1, len(df)):
        if close.iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = "UP"
            supertrend.iloc[i] = lower_band.iloc[i]
        elif close.iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = "DOWN"
            supertrend.iloc[i] = upper_band.iloc[i]
        else:
            direction.iloc[i] = direction.iloc[i - 1] if i > 0 else "UP"
            supertrend.iloc[i] = (
                lower_band.iloc[i] if direction.iloc[i] == "UP"
                else upper_band.iloc[i]
            )

    df = df.copy()
    df["supertrend_val"] = supertrend
    df["supertrend_dir"] = direction
    return df


# ── EMA ───────────────────────────────────────────────────────

def compute_ema(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA fast and slow columns to dataframe."""
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()
    return df


# ── RSI ───────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI column to dataframe."""
    df = df.copy()
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(span=config.RSI_PERIOD, adjust=False).mean()
    avg_l  = loss.ewm(span=config.RSI_PERIOD, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)
    return df


# ── VWAP ──────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute intraday VWAP.
    Groups by date so VWAP resets at the start of each trading day.
    """
    df = df.copy()
    df["_typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["_date"]    = pd.to_datetime(df["datetime"]).dt.date

    def _vwap_group(g: pd.DataFrame) -> pd.Series:
        cum_vol = g["volume"].cumsum()
        cum_tp  = (g["_typical"] * g["volume"]).cumsum()
        return cum_tp / cum_vol.replace(0, np.nan)

    df["vwap"] = df.groupby("_date", group_keys=False).apply(_vwap_group)
    df["vwap"] = df["vwap"].ffill()
    df.drop(columns=["_typical", "_date"], inplace=True)
    return df


# ── Master Signal Generator ───────────────────────────────────

def generate_signal(df: pd.DataFrame, vix: float = 0.0) -> StrategyResult:
    """
    Run all indicators on OHLC candle data and generate a trading signal.

    Parameters:
        df  : OHLC DataFrame (from market_data.get_nifty_candles)
        vix : Current India VIX value

    Returns:
        StrategyResult with signal, option_type, confidence score, and reason.

    Algorithm:
        1. Compute Supertrend, EMA, RSI, VWAP on the candle data
        2. Take the LAST completed candle (index -2; -1 is forming)
        3. Score each indicator: +1 for bullish, -1 for bearish, 0 for neutral
        4. Confidence = count of agreeing indicators (max 5)
        5. BUY CE if score >= +3  (majority bullish)
        6. BUY PE if score <= -3  (majority bearish)
        7. HOLD otherwise
    """
    # ── 1. Compute all indicators ─────────────────────────────
    df = compute_supertrend(df)
    df = compute_ema(df)
    df = compute_rsi(df)
    df = compute_vwap(df)
    df = df.dropna().reset_index(drop=True)

    if len(df) < max(config.EMA_SLOW, config.RSI_PERIOD) + 5:
        logger.warning("Not enough candle data for reliable signal — HOLD")
        return StrategyResult(
            signal=Signal.HOLD, option_type=None, confidence=0,
            supertrend="UNKNOWN", rsi=50, ema_fast=0, ema_slow=0,
            vwap=0, close=0, reason="Insufficient candle data"
        )

    # ── 2. Use last COMPLETED candle (avoid forming candle) ───
    row = df.iloc[-2]

    close    = row["close"]
    st_dir   = row["supertrend_dir"]
    ema_fast = row["ema_fast"]
    ema_slow = row["ema_slow"]
    rsi      = row["rsi"]
    vwap     = row["vwap"]

    # ── 3. Score each indicator ───────────────────────────────
    score = 0
    reasons = []

    # Supertrend
    if st_dir == "UP":
        score += 1
        reasons.append("Supertrend↑")
    else:
        score -= 1
        reasons.append("Supertrend↓")

    # EMA crossover
    if ema_fast > ema_slow:
        score += 1
        reasons.append(f"EMA9({ema_fast:.1f})>EMA21({ema_slow:.1f})")
    else:
        score -= 1
        reasons.append(f"EMA9({ema_fast:.1f})<EMA21({ema_slow:.1f})")

    # RSI — neutral zone avoids extremes
    if 45 <= rsi <= 65:
        score += 1
        reasons.append(f"RSI({rsi:.1f}) in zone")
    elif rsi > config.RSI_OVERBOUGHT:
        score -= 1
        reasons.append(f"RSI({rsi:.1f}) overbought")
    elif rsi < config.RSI_OVERSOLD:
        score -= 1
        reasons.append(f"RSI({rsi:.1f}) oversold")
    else:
        reasons.append(f"RSI({rsi:.1f}) neutral")

    # VWAP
    if close > vwap:
        score += 1
        reasons.append(f"Close({close:.1f})>VWAP({vwap:.1f})")
    else:
        score -= 1
        reasons.append(f"Close({close:.1f})<VWAP({vwap:.1f})")

    # VIX filter (veto — if VIX too high, override to HOLD regardless)
    if vix > config.VIX_MAX_THRESHOLD:
        logger.warning(f"🚫 VIX={vix:.1f} > {config.VIX_MAX_THRESHOLD} — HOLD (too volatile)")
        return StrategyResult(
            signal=Signal.HOLD, option_type=None, confidence=0,
            supertrend=st_dir, rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow,
            vwap=vwap, close=close,
            reason=f"VIX={vix:.1f} exceeds threshold — skipping trade"
        )

    # ── 4. Determine signal ───────────────────────────────────
    confidence = abs(score)   # 0–4 (max 4 contributing indicators)

    if score >= 3:
        signal      = Signal.BUY
        option_type = OptionType.CE
        reason = f"BULLISH [{', '.join(reasons)}] score={score}/4"
    elif score <= -3:
        signal      = Signal.BUY
        option_type = OptionType.PE
        reason = f"BEARISH [{', '.join(reasons)}] score={score}/4"
    else:
        signal      = Signal.HOLD
        option_type = None
        reason = f"MIXED signals [{', '.join(reasons)}] score={score}/4 — no trade"

    logger.info(f"📊 Signal: {signal.value} {option_type.value if option_type else '—'} | {reason}")

    return StrategyResult(
        signal=signal,
        option_type=option_type,
        confidence=confidence,
        supertrend=st_dir,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap=vwap,
        close=close,
        reason=reason,
    )


# ── Exit Signal Checker ───────────────────────────────────────

def should_exit(
    entry_price: float,
    current_ltp: float,
    df: pd.DataFrame,
) -> tuple[bool, str]:
    """
    Determine if an open trade should be exited.

    Checks (in priority order):
      1. Target hit  (+PROFIT_TARGET_POINTS)
      2. Stop-loss   (-STOP_LOSS_POINTS)
      3. Supertrend direction flip (early exit)

    Returns:
        (True, reason)  → exit now
        (False, "")     → hold position
    """
    pnl_points = current_ltp - entry_price

    # 1. Target
    if pnl_points >= config.PROFIT_TARGET_POINTS:
        return True, f"TARGET +{pnl_points:.1f} pts"

    # 2. Stop-loss
    if pnl_points <= -config.STOP_LOSS_POINTS:
        return True, f"STOPLOSS {pnl_points:.1f} pts"

    # 3. Supertrend flip (early exit to protect capital)
    try:
        df_st = compute_supertrend(df)
        last_dir = df_st["supertrend_dir"].iloc[-1]
        prev_dir = df_st["supertrend_dir"].iloc[-2]
        if prev_dir == "UP" and last_dir == "DOWN":
            return True, f"SUPERTREND FLIP (UP→DOWN) P&L={pnl_points:.1f} pts"
        if prev_dir == "DOWN" and last_dir == "UP":
            return True, f"SUPERTREND FLIP (DOWN→UP) P&L={pnl_points:.1f} pts"
    except Exception:
        pass  # not enough data for flip check — skip

    return False, ""
