"""
market_data.py — Live market data fetcher via Angel One SmartAPI.

Provides:
  - get_ltp()           : Last Traded Price for any symbol
  - get_ohlc_candles()  : Historical OHLC candles (used to compute indicators)
  - get_india_vix()     : India VIX value (volatility filter)
  - get_option_chain()  : OI + PCR data for Nifty options
  - get_atm_strike()    : Auto-compute ATM strike from current Nifty spot
  - get_symbol_token()  : Resolve symbol name → exchange token
"""

import time
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
from loguru import logger

import auth
import config

# ── NSE Exchange Segment Codes (Angel One) ────────────────────
NSE_CM  = "NSE"      # Cash market (equities)
NSE_FO  = "NFO"      # Futures & Options
NSE_IDX = "NSE"      # Index (Nifty spot price)

# ── Known tokens for common symbols ──────────────────────────
_TOKEN_CACHE: dict[str, str] = {}

# Nifty 50 index token (Angel One hardcoded token)
NIFTY_INDEX_TOKEN = "99926000"
INDIA_VIX_TOKEN   = "99919000"


def get_symbol_token(symbol: str, exchange: str = NSE_FO) -> str:
    """
    Resolve a trading symbol string to its Angel One exchange token.
    Caches results to avoid redundant API calls.
    """
    cache_key = f"{exchange}:{symbol}"
    if cache_key in _TOKEN_CACHE:
        return _TOKEN_CACHE[cache_key]

    smart = auth.get_session()
    try:
        resp = smart.searchScrip(exchange=exchange, searchscrip=symbol)
        if resp["status"] and resp["data"]:
            token = resp["data"][0]["symboltoken"]
            _TOKEN_CACHE[cache_key] = token
            return token
    except Exception as exc:
        logger.error(f"Token lookup failed for {symbol}: {exc}")

    raise ValueError(f"Could not resolve token for {symbol} on {exchange}")


def get_ltp(symbol: str, token: str, exchange: str = NSE_FO) -> float:
    """
    Fetch the Last Traded Price (LTP) for a symbol.
    Used inside the main loop to track live option premium.
    """
    smart = auth.get_session()
    resp = smart.ltpData(exchange=exchange, tradingsymbol=symbol, symboltoken=token)
    if not resp.get("status"):
        raise RuntimeError(f"LTP fetch failed for {symbol}: {resp.get('message')}")
    return float(resp["data"]["ltp"])


def get_nifty_spot() -> float:
    """Return current Nifty 50 spot/index price."""
    smart = auth.get_session()
    resp = smart.ltpData(
        exchange=NSE_IDX,
        tradingsymbol="Nifty 50",
        symboltoken=NIFTY_INDEX_TOKEN
    )
    if not resp.get("status"):
        raise RuntimeError(f"Nifty spot fetch failed: {resp.get('message')}")
    return float(resp["data"]["ltp"])


def get_india_vix() -> float:
    """Return current India VIX value."""
    smart = auth.get_session()
    resp = smart.ltpData(
        exchange=NSE_IDX,
        tradingsymbol="India VIX",
        symboltoken=INDIA_VIX_TOKEN
    )
    if not resp.get("status"):
        logger.warning("India VIX fetch failed — defaulting to 0 (no filter)")
        return 0.0
    return float(resp["data"]["ltp"])


def get_atm_strike(spot: float, strike_gap: int = 50) -> int:
    """
    Round the Nifty spot price to the nearest ATM (At The Money) strike.
    Nifty strikes are in multiples of 50.

    Example: spot=19847 → ATM=19850
    """
    return round(spot / strike_gap) * strike_gap


def get_ohlc_candles(
    symbol: str,
    token: str,
    exchange: str = NSE_FO,
    interval: str = "FIVE_MINUTE",
    lookback_days: int = 5
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candle data for indicator computation.

    Interval options: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE,
                      TEN_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE,
                      ONE_HOUR, ONE_DAY

    Returns a DataFrame with columns: datetime, open, high, low, close, volume
    """
    smart = auth.get_session()
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=lookback_days)

    params = {
        "exchange":    exchange,
        "symboltoken": token,
        "interval":    interval,
        "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
    }

    resp = smart.getCandleData(params)
    if not resp.get("status") or not resp.get("data"):
        raise RuntimeError(f"Candle data fetch failed for {symbol}: {resp.get('message')}")

    df = pd.DataFrame(
        resp["data"],
        columns=["datetime", "open", "high", "low", "close", "volume"]
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df[["open", "high", "low", "close", "volume"]] = df[
        ["open", "high", "low", "close", "volume"]
    ].apply(pd.to_numeric)
    return df


def get_nifty_candles(interval: str = "FIVE_MINUTE", lookback_days: int = 5) -> pd.DataFrame:
    """Convenience wrapper — fetch Nifty 50 index OHLC candles."""
    return get_ohlc_candles(
        symbol="Nifty 50",
        token=NIFTY_INDEX_TOKEN,
        exchange=NSE_IDX,
        interval=interval,
        lookback_days=lookback_days,
    )


def build_option_symbol(
    underlying: str,
    expiry: str,
    strike: int,
    option_type: str
) -> str:
    """
    Build the Angel One NSE option trading symbol string.

    Args:
        underlying:  "NIFTY"
        expiry:      "23SEP"  (format: YYMonthDD for weekly, or YYMON for monthly)
        strike:      19850
        option_type: "CE" or "PE"

    Returns: "NIFTY23SEP1985CE"  (example)
    """
    return f"{underlying}{expiry}{strike}{option_type}"


# ── BLOCKER 5 FIX: OI Liquidity check ────────────────────────

MIN_OI_THRESHOLD = 100_000   # minimum Open Interest to consider a strike liquid

def check_liquidity(symbol: str, token: str, exchange: str = NSE_FO) -> tuple[bool, str]:
    """
    Check if an option strike has sufficient Open Interest for safe trading.

    Low OI = wide bid-ask spread = guaranteed slippage loss at entry.
    Minimum threshold: 100,000 OI (configurable via MIN_OI env var).

    Returns:
        (True, "")           — liquid, safe to trade
        (False, reason)      — illiquid, skip this strike
    """
    import os
    min_oi = int(os.getenv("MIN_OI_THRESHOLD", str(MIN_OI_THRESHOLD)))

    try:
        smart = auth.get_session()
        resp  = smart.ltpData(exchange=exchange, tradingsymbol=symbol, symboltoken=token)
        if not resp.get("status"):
            # Can't verify — allow trade but warn
            logger.warning(f"Liquidity check failed for {symbol} — proceeding anyway")
            return True, ""

        data = resp.get("data", {})

        # Angel One LTP response includes OI field
        oi = int(data.get("opninterest", 0) or data.get("oi", 0) or 0)

        if oi == 0:
            # OI not returned in LTP — try market depth
            logger.debug(f"OI not in LTP for {symbol} — skipping liquidity gate")
            return True, ""

        if oi < min_oi:
            reason = f"Low OI={oi:,} < {min_oi:,} — spread risk too high"
            logger.warning(f"⚠️ {symbol} ILLIQUID: {reason}")
            return False, reason

        logger.debug(f"✅ {symbol} liquid: OI={oi:,}")
        return True, ""

    except Exception as exc:
        logger.warning(f"Liquidity check exception: {exc} — allowing trade")
        return True, ""
