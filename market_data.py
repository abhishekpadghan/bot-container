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

# ── Exchange codes ────────────────────────────────────────────
NSE_CM  = "NSE"      # Cash market
NSE_FO  = "NFO"      # NSE Futures & Options
BSE_FO  = "BFO"      # BSE Futures & Options (Sensex/Bankex)
NSE_IDX = "NSE"      # NSE Index

# ── Token cache ───────────────────────────────────────────────
_TOKEN_CACHE: dict[str, str] = {}

# ── Instrument Configuration Registry ────────────────────────
# All per-instrument settings in one place.
# Add new instruments here — bot picks them up automatically.

from dataclasses import dataclass
from typing import Optional as Opt

@dataclass
class InstrumentConfig:
    name:           str     # symbol prefix, e.g. "NIFTY", "SENSEX"
    index_token:    str     # Angel One token for the spot index
    index_symbol:   str     # Display name for ltpData call
    index_exchange: str     # exchange for index LTP
    fo_exchange:    str     # exchange for options chain
    lot_size:       int     # units per lot
    strike_gap:     int     # distance between strikes (pts)
    expiry_weekday: int     # 0=Mon … 6=Sun (day of weekly expiry)
    expiry_type:    str     # "weekly" | "monthly"
    # per-instrument risk overrides (None = use global config)
    profit_pts:     Opt[float] = None
    sl_pts:         Opt[float] = None

    @property
    def effective_profit_pts(self) -> float:
        import config
        return self.profit_pts or config.PROFIT_TARGET_POINTS

    @property
    def effective_sl_pts(self) -> float:
        import config
        return self.sl_pts or config.STOP_LOSS_POINTS


# ── Registry of all supported instruments ────────────────────
#
# Index token: "99926000" for NIFTY is a well-known Angel One constant.
# SENSEX token is resolved via instrument master (searchScrip) at runtime —
# we use "1" as the BSE Sensex scrip code but the bot also falls back to
# searchScrip if LTP fails, so the exact token is validated at startup.
#
# Source for lot sizes and expiry days: NSE/BSE circulars (as of 2025-26):
#   NIFTY 50   → lot 65,  Tuesday weekly          (NSE)
#   SENSEX     → lot 20,  Thursday weekly          (BSE)
#   BANKNIFTY  → lot 30,  last Tuesday monthly     (NSE)
#   BANKEX     → lot 30,  last Thursday monthly    (BSE)
#   FINNIFTY   → lot 60,  last Tuesday monthly     (NSE)
#
# expiry_type="monthly" means the LAST occurrence of expiry_weekday in the
# current (or next, if already past) contract month — computed in
# get_expiry_string() below.
#
# ⚠️ Always verify current lot sizes on NSE/BSE website before live trading.
# Override any lot size via env: NIFTY_LOT_SIZE=65  SENSEX_LOT_SIZE=20

import os as _os

def _lot(instrument: str, default: int) -> int:
    """Allow per-instrument lot size override via environment variable."""
    return int(_os.getenv(f"{instrument}_LOT_SIZE", str(default)))


INSTRUMENT_REGISTRY: dict[str, InstrumentConfig] = {

    # ── NIFTY 50 (NSE) ────────────────────────────────────────
    "NIFTY": InstrumentConfig(
        name           = "NIFTY",
        index_token    = "99926000",     # Angel One hardcoded NSE Nifty 50 token
        index_symbol   = "Nifty 50",
        index_exchange = "NSE",
        fo_exchange    = "NFO",
        lot_size       = _lot("NIFTY", 65),   # ✅ corrected: 65 units (revised 2025)
        strike_gap     = 50,
        expiry_weekday = 1,              # ✅ Tuesday (revised from Thursday)
        expiry_type    = "weekly",
    ),

    # ── SENSEX (BSE) ──────────────────────────────────────────
    "SENSEX": InstrumentConfig(
        name           = "SENSEX",
        index_token    = "1",            # BSE Sensex scrip code; token resolved via searchScrip
        index_symbol   = "SENSEX",
        index_exchange = "BSE",
        fo_exchange    = "BFO",
        lot_size       = _lot("SENSEX", 20),  # 20 units (BSE)
        strike_gap     = 100,
        expiry_weekday = 3,              # Thursday — weekly (BSE)
        expiry_type    = "weekly",
        profit_pts     = 25,             # Strategy: 25 / 12 pts
        sl_pts         = 12,
    ),

    # ── BANKNIFTY (NSE) ───────────────────────────────────────
    "BANKNIFTY": InstrumentConfig(
        name           = "BANKNIFTY",
        index_token    = "99926009",
        index_symbol   = "Nifty Bank",
        index_exchange = "NSE",
        fo_exchange    = "NFO",
        lot_size       = _lot("BANKNIFTY", 30),  # 30 units (NSE)
        strike_gap     = 100,
        expiry_weekday = 1,              # Tuesday — last Tuesday of month (monthly)
        expiry_type    = "monthly",
        profit_pts     = 30,             # Strategy: 30 / 15 pts
        sl_pts         = 15,
    ),

    # ── BANKEX (BSE) ──────────────────────────────────────────
    "BANKEX": InstrumentConfig(
        name           = "BANKEX",
        index_token    = "1",            # BSE Bankex — token resolved via searchScrip
        index_symbol   = "BANKEX",
        index_exchange = "BSE",
        fo_exchange    = "BFO",
        lot_size       = _lot("BANKEX", 30),    # 30 units (BSE, revised from 15)
        strike_gap     = 100,
        expiry_weekday = 3,              # Thursday — last Thursday of month (monthly)
        expiry_type    = "monthly",
        profit_pts     = 30,             # Strategy: 30 / 15 pts
        sl_pts         = 15,
    ),

    # ── FINNIFTY (NSE) ────────────────────────────────────────
    "FINNIFTY": InstrumentConfig(
        name           = "FINNIFTY",
        index_token    = "99926037",
        index_symbol   = "Nifty Fin Service",
        index_exchange = "NSE",
        fo_exchange    = "NFO",
        lot_size       = _lot("FINNIFTY", 60),  # 60 units (NSE, revised from 40)
        strike_gap     = 50,
        expiry_weekday = 1,              # Tuesday — last Tuesday of month (monthly)
        expiry_type    = "monthly",
        profit_pts     = 20,             # Strategy: 20 / 10 pts
        sl_pts         = 10,
    ),
}

# ── Backward-compatible token constants ───────────────────────
NIFTY_INDEX_TOKEN = INSTRUMENT_REGISTRY["NIFTY"].index_token
INDIA_VIX_TOKEN   = "99919000"


def get_instrument(name: str) -> InstrumentConfig:
    """Return InstrumentConfig for a given instrument name. Raises ValueError if unknown."""
    key = name.strip().upper()
    if key not in INSTRUMENT_REGISTRY:
        raise ValueError(
            f"Unknown instrument '{key}'. "
            f"Supported: {', '.join(INSTRUMENT_REGISTRY.keys())}"
        )
    return INSTRUMENT_REGISTRY[key]


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
    """Convenience wrapper — fetch Nifty 50 index OHLC candles (backward compat)."""
    return get_index_candles("NIFTY", interval=interval, lookback_days=lookback_days)


def get_index_candles(
    instrument_name: str,
    interval: str = "FIVE_MINUTE",
    lookback_days: int = 5,
) -> pd.DataFrame:
    """
    Fetch OHLC candles for any supported instrument by name.

    Usage:
        get_index_candles("NIFTY")
        get_index_candles("SENSEX")
        get_index_candles("BANKNIFTY", interval="ONE_MINUTE")
    """
    inst = get_instrument(instrument_name)
    return get_ohlc_candles(
        symbol=inst.index_symbol,
        token=inst.index_token,
        exchange=inst.index_exchange,
        interval=interval,
        lookback_days=lookback_days,
    )


def get_spot(instrument_name: str) -> float:
    """
    Fetch current spot/index price for any supported instrument.

    Usage:
        get_spot("NIFTY")    → 24350.0
        get_spot("SENSEX")   → 80200.0
        get_spot("BANKNIFTY") → 52100.0
    """
    inst  = get_instrument(instrument_name)
    smart = auth.get_session()
    resp  = smart.ltpData(
        exchange=inst.index_exchange,
        tradingsymbol=inst.index_symbol,
        symboltoken=inst.index_token,
    )
    if not resp.get("status"):
        raise RuntimeError(
            f"{instrument_name} spot fetch failed: {resp.get('message')}"
        )
    return float(resp["data"]["ltp"])


def get_nifty_spot() -> float:
    """Backward-compatible wrapper."""
    return get_spot("NIFTY")


def _last_weekday_of_month(year: int, month: int, weekday: int) -> "date":
    """
    Return the date of the LAST occurrence of `weekday` (0=Mon…6=Sun)
    in the given year/month.  Used for monthly expiry instruments.
    """
    import calendar
    from datetime import date as _date
    # last day of month
    last_day = calendar.monthrange(year, month)[1]
    last_date = _date(year, month, last_day)
    # walk backwards to find the target weekday
    delta = (last_date.weekday() - weekday) % 7
    return last_date - timedelta(days=delta)


def get_expiry_string(instrument_name: str) -> str:
    """
    Build the Angel One expiry string for any instrument.

    Weekly instruments  → nearest upcoming expiry_weekday
                          format: YYMONDD  e.g. "25JUL29"
    Monthly instruments → last occurrence of expiry_weekday in the
                          current contract month (rolls to next month
                          if today is past that date)
                          format: YYMON    e.g. "25JUL"

    Current schedule (2025-26):
      NIFTY     → Tuesday  weekly   → "25JUL29"
      SENSEX    → Thursday weekly   → "25JUL31"
      BANKNIFTY → last Tue monthly  → "25JUL"
      BANKEX    → last Thu monthly  → "25JUL"
      FINNIFTY  → last Tue monthly  → "25JUL"
    """
    inst = get_instrument(instrument_name)
    now  = datetime.now()

    if inst.expiry_type == "weekly":
        target_dow = inst.expiry_weekday
        days_ahead = (target_dow - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7          # already on expiry day → use next week's
        expiry_dt = now + timedelta(days=days_ahead)
        return expiry_dt.strftime("%y%b%d").upper()

    else:  # monthly — last occurrence of expiry_weekday in month
        last_exp = _last_weekday_of_month(now.year, now.month, inst.expiry_weekday)
        # if today is on or past the last expiry, roll to next month
        if now.date() >= last_exp:
            # advance to the 1st of next month then find last weekday there
            if now.month == 12:
                next_year, next_month = now.year + 1, 1
            else:
                next_year, next_month = now.year, now.month + 1
            last_exp = _last_weekday_of_month(next_year, next_month, inst.expiry_weekday)
        return last_exp.strftime("%y%b").upper()


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
