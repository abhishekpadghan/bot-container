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

# Short-TTL negative cache: symbols that returned AB4047 this session.
# Prevents hammering searchScrip every tick for the same missing scrip.
# Key: "exchange:symbol"  Value: monotonic timestamp of the failure.
_TOKEN_MISS_CACHE: dict[str, float] = {}
_TOKEN_MISS_TTL = 120   # seconds — retry AB4047 symbols after 2 minutes

# ── Candle cache ──────────────────────────────────────────────────────
# getCandleData (historical endpoint) has a STRICTER quota than ltpData:
# Angel One allows only ~3 historical calls per minute per API key.
# Fix: cache candles for _CANDLE_TTL seconds so getCandleData fires at most
# once per minute regardless of poll interval.  ltpData (spot/LTP) is always
# live and is NOT cached — every get_spot() / get_ltp() call fires a fresh
# ltpData request so the dashboard shows real-time index movement.
import threading as _threading
_CANDLE_CACHE: dict[str, tuple] = {}
_CANDLE_CACHE_LOCK = _threading.Lock()
_CANDLE_TTL = 62   # seconds — cache candles for 62s (just over 1 min)
                   # Angel One getCandleData quota: ~3 calls/min rolling window.
                   # 62s TTL guarantees at most 1 getCandleData call per window
                   # regardless of POLL_INTERVAL (even if set to 30s in .env).

# ── Global API rate-limit throttle ───────────────────────────────────
# Angel One enforces AB1021 at >3 calls per 10-second rolling window.
#
# Two separate delays are used:
#   _LTP_CALL_DELAY     = 1.5s  — for ltpData calls (spot, ltp, vix)
#   _CANDLE_CALL_DELAY  = 4.0s  — for getCandleData (historical, strict quota)
#
# A shared timestamp (_last_api_call_t) ensures the gap is measured from
# when the PREVIOUS request was sent, not when it returned — guaranteeing
# the inter-call gap on Angel One's servers, not just in our process.
_API_LOCK          = _threading.Lock()
_LTP_CALL_DELAY    = 1.5   # seconds between ltpData calls (lax quota)
_CANDLE_CALL_DELAY = 6.0   # seconds before getCandleData (strict ~1 req/min quota)
                            # Increased from 4s to 6s: gives more breathing room
                            # when ltpData calls are interleaved in the same window.
_last_api_call_t   = 0.0   # monotonic timestamp of the last API call sent (module-level)


def _api_throttle(delay: float = _LTP_CALL_DELAY) -> None:
    """Enforce minimum gap between API calls. MUST be called while holding _API_LOCK."""
    global _last_api_call_t
    elapsed = time.monotonic() - _last_api_call_t
    wait    = delay - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_api_call_t = time.monotonic()

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
        index_token    = "99919012",     # ✅ BSE Sensex index token (Angel One scrip master)
        index_symbol   = "BSE Sensex",  # ✅ exact display name Angel One uses for ltpData
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
        index_token    = "99919013",     # ✅ BSE Bankex index token (Angel One scrip master)
        index_symbol   = "BSE Bankex",  # ✅ exact display name Angel One uses for ltpData
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

# India VIX token — resolved at runtime via searchScrip if the
# hardcoded token is rejected (Angel One changed their scrip master).
# Hardcoded value kept as first attempt; fallback searches for it.
INDIA_VIX_TOKEN   = "99919000"    # may be stale — see _resolve_vix_token()
_vix_token_resolved: str | None = None


def _resolve_vix_token() -> str:
    """
    Attempt to resolve the Angel One token for India VIX.

    Angel One's SmartAPI does NOT reliably expose India VIX via ltpData —
    their scrip master cache for VIX is inconsistent across sessions.
    The correct approach is to use India VIX from NSE website or a separate
    data source, but for now we disable VIX filtering and return "" so the
    bot trades without it (VIX filter defaults to 0 = no block).

    If/when Angel One fixes this, update INDIA_VIX_TOKEN with the correct
    token from their instrument master CSV.
    """
    global _vix_token_resolved
    if _vix_token_resolved is not None:
        return _vix_token_resolved

    # Try the hardcoded token — verify the returned value is in VIX range (5–50)
    smart = auth.get_session()
    try:
        resp = smart.ltpData(exchange="NSE", tradingsymbol="India VIX",
                             symboltoken=INDIA_VIX_TOKEN)
        if resp.get("status") and resp.get("data"):
            val = float(resp["data"].get("ltp", 0))
            if 5.0 <= val <= 50.0:   # sanity check: VIX is always in this range
                logger.info(f"✅ India VIX token OK: {INDIA_VIX_TOKEN} → {val}")
                _vix_token_resolved = INDIA_VIX_TOKEN
                return INDIA_VIX_TOKEN
            else:
                logger.warning(
                    f"⚠️  India VIX token {INDIA_VIX_TOKEN} returned suspicious "
                    f"value {val} — disabling VIX filter"
                )
    except Exception:
        pass

    # Token not working — disable VIX filter gracefully
    logger.warning("⚠️  India VIX unavailable via SmartAPI — VIX filter disabled (trades freely)")
    _vix_token_resolved = ""
    return ""


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

    AB4047 handling: if the scrip is not in Angel One's in-memory cache
    (common after mid-session for new weekly contracts), we force a session
    re-login which refreshes the scrip master, then retry once.
    Failed lookups are cached for _TOKEN_MISS_TTL seconds so we don't
    burn quota re-trying the same missing scrip every tick.
    """
    cache_key = f"{exchange}:{symbol}"

    # Positive cache hit
    if cache_key in _TOKEN_CACHE:
        return _TOKEN_CACHE[cache_key]

    # Negative cache: still within miss TTL — skip the API call entirely
    if cache_key in _TOKEN_MISS_CACHE:
        elapsed = time.monotonic() - _TOKEN_MISS_CACHE[cache_key]
        if elapsed < _TOKEN_MISS_TTL:
            raise ValueError(f"Could not resolve token for {symbol} on {exchange} (cached miss)")

    smart = auth.get_session()
    for attempt in range(1, 3):   # max 2 attempts: one normal, one after re-login
        try:
            with _API_LOCK:
                _api_throttle(delay=_CANDLE_CALL_DELAY)   # searchScrip = strict quota
                resp = smart.searchScrip(exchange=exchange, searchscrip=symbol)

            if resp.get("status") and resp.get("data"):
                token = resp["data"][0]["symboltoken"]
                _TOKEN_CACHE[cache_key] = token
                # Clear any stale negative cache entry on success
                _TOKEN_MISS_CACHE.pop(cache_key, None)
                return token

            # AB4047: scrip not in session's scrip master cache
            # Force re-login to refresh the instrument master, then retry
            errorcode = resp.get("errorcode", "")
            if errorcode == "AB4047" and attempt == 1:
                logger.warning(
                    f"AB4047 for {symbol} — scrip master stale, refreshing session..."
                )
                from session_manager import SessionManager
                SessionManager.get()._relogin()
                smart = auth.get_session()   # get fresh session after re-login
                continue   # retry with fresh session

            # Any other non-status response — stop
            logger.error(f"searchScrip failed for {symbol}: {resp.get('message')}")
            break

        except Exception as exc:
            logger.error(f"Token lookup failed for {symbol}: {exc}")
            break

    # Cache the failure so we don't retry every tick
    _TOKEN_MISS_CACHE[cache_key] = time.monotonic()
    raise ValueError(f"Could not resolve token for {symbol} on {exchange}")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if this exception is an Angel One rate-limit response."""
    s = str(exc).lower()
    return any(x in s for x in [
        "access rate", "too many", "couldn't parse", "parse the json",
        "ab1021", "ab1010",
    ])


def get_ltp(symbol: str, token: str, exchange: str = NSE_FO) -> float:
    """
    Fetch the Last Traded Price (LTP) for a symbol.
    Used inside the main loop to track live option premium.
    """
    smart = auth.get_session()
    try:
        with _API_LOCK:
            _api_throttle()
            resp = smart.ltpData(exchange=exchange, tradingsymbol=symbol, symboltoken=token)
    except Exception as exc:
        if _is_rate_limit_error(exc):
            logger.warning(f"[Rate limit] ltpData for {symbol} — waiting 10s: {exc}")
            time.sleep(10)
            raise RuntimeError(f"LTP rate-limited for {symbol}: {exc}")
        raise RuntimeError(f"LTP fetch exception for {symbol}: {exc}")
    if not resp.get("status"):
        raise RuntimeError(f"LTP fetch failed for {symbol}: {resp.get('message')}")
    return float(resp["data"]["ltp"])


def get_nifty_spot() -> float:
    """Return current Nifty 50 spot/index price. Delegates to get_spot() for lock+delay."""
    return get_spot("NIFTY")


def get_india_vix() -> float:
    """
    Return current India VIX value.
    Uses _resolve_vix_token() to handle Angel One scrip master cache
    changes (token 99919000 may not always be in the cache — AB4046).
    Returns 0.0 on any failure so the VIX filter is simply disabled.
    """
    token = _resolve_vix_token()
    if not token:
        return 0.0
    smart = auth.get_session()
    try:
        with _API_LOCK:
            _api_throttle()
            resp = smart.ltpData(
                exchange=NSE_IDX,
                tradingsymbol="India VIX",
                symboltoken=token
            )
        if not resp.get("status") or not resp.get("data"):
            logger.warning("India VIX fetch failed — defaulting to 0 (no filter)")
            return 0.0
        return float(resp["data"]["ltp"])
    except Exception as exc:
        logger.warning(f"India VIX exception: {exc} — defaulting to 0")
        return 0.0


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

    Rate-limit note: Angel One allows ~1 req/sec.  The _CANDLE_CALL_DELAY sleep
    before the request prevents AB1010 "Access denied: exceeding access rate".
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

    # Rate-limit guard — use CANDLE_CALL_DELAY (4s) not LTP delay (1.5s)
    # getCandleData has a stricter ~1 req/min quota vs ltpData.
    #
    # Angel One SmartAPI library raises an EXCEPTION (not a dict response)
    # when it receives a non-JSON rate-limit reply like:
    #   b'Access denied because of exceeding access rate'
    # The exception message contains "Couldn't parse the JSON response".
    # We must catch that at the try/except level and back off, not just
    # check isinstance(raw, bytes) after the call.
    # Single attempt with one retry after a fixed wait.
    # Do NOT loop 3× with growing sleeps — that blocks the main thread for
    # up to 90s and freezes all logging. If rate-limited, wait once (20s)
    # then retry. If still failing, raise so the caller can use cached data.
    resp = None
    for attempt in range(1, 3):   # max 2 attempts: normal + one retry
        try:
            with _API_LOCK:
                _api_throttle(delay=_CANDLE_CALL_DELAY)
                raw = smart.getCandleData(params)
        except Exception as lib_exc:
            err_str = str(lib_exc).lower()
            is_rate_limit = any(x in err_str for x in [
                "access rate", "too many", "couldn't parse", "parse the json",
                "ab1021", "ab1010",
            ])
            if is_rate_limit and attempt == 1:
                logger.warning(
                    f"[Rate limit] getCandleData for {symbol} — waiting 20s then retrying. "
                    f"Error: {lib_exc}"
                )
                time.sleep(20)
                continue   # one retry
            raise RuntimeError(
                f"getCandleData {'rate-limited' if is_rate_limit else 'failed'} "
                f"for {symbol}: {lib_exc}"
            )

        # getCandleData returned — check for raw bytes (older SmartAPI versions)
        if isinstance(raw, (bytes, str)):
            body = raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
            if ("access rate" in body.lower() or "too many" in body.lower()) and attempt == 1:
                logger.warning(
                    f"[Rate limit bytes] getCandleData for {symbol} — waiting 20s then retrying"
                )
                time.sleep(20)
                continue
            raise RuntimeError(f"Couldn't parse candle response for {symbol}: {body[:80]}")

        resp = raw
        break  # clean dict response — done

    # status=True but data=None → market just opened, no candles yet
    # Raise a clear, specific error so callers can skip this tick gracefully
    if not resp.get("status"):
        msg = resp.get("message", "unknown")
        raise RuntimeError(f"Candle data fetch failed for {symbol}: {msg}")

    if not resp.get("data"):
        raise RuntimeError(
            f"No candle data yet for {symbol} — market may have just opened, "
            "will retry next tick"
        )

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
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch OHLC candles for any supported instrument by name.

    Results are cached for _CANDLE_TTL seconds to prevent hitting Angel One's
    rate limit when the same interval is requested multiple times per tick.
    Pass use_cache=False to force a fresh fetch.

    Usage:
        get_index_candles("NIFTY")
        get_index_candles("SENSEX")
        get_index_candles("BANKNIFTY", interval="ONE_MINUTE")
    """
    cache_key = f"{instrument_name.upper()}:{interval}:{lookback_days}"

    if use_cache:
        with _CANDLE_CACHE_LOCK:
            if cache_key in _CANDLE_CACHE:
                fetched_at, cached_df = _CANDLE_CACHE[cache_key]
                if time.time() - fetched_at < _CANDLE_TTL:
                    return cached_df  # fresh enough — skip the API call

    inst = get_instrument(instrument_name)
    try:
        df = get_ohlc_candles(
            symbol=inst.index_symbol,
            token=inst.index_token,
            exchange=inst.index_exchange,
            interval=interval,
            lookback_days=lookback_days,
        )
    except RuntimeError as exc:
        # On rate-limit: serve stale cache if available rather than crashing the tick.
        # Stale candles are better than no candles — indicators still work, just
        # slightly behind. The tick continues and next poll will get fresh data.
        if "rate-limited" in str(exc).lower() or "rate limit" in str(exc).lower():
            with _CANDLE_CACHE_LOCK:
                if cache_key in _CANDLE_CACHE:
                    _, stale_df = _CANDLE_CACHE[cache_key]
                    logger.warning(
                        f"[{instrument_name}] getCandleData rate-limited — "
                        f"serving stale candles from cache. Bot continues."
                    )
                    return stale_df
        raise   # no cache — propagate so tick is skipped cleanly

    with _CANDLE_CACHE_LOCK:
        _CANDLE_CACHE[cache_key] = (time.time(), df)
    return df


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
    try:
        with _API_LOCK:
            _api_throttle()
            resp = smart.ltpData(
                exchange=inst.index_exchange,
                tradingsymbol=inst.index_symbol,
                symboltoken=inst.index_token,
            )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            logger.warning(f"[Rate limit] spot ltpData for {instrument_name} — waiting 10s: {exc}")
            time.sleep(10)
            raise RuntimeError(f"Spot rate-limited for {instrument_name}: {exc}")
        raise RuntimeError(f"Spot fetch exception for {instrument_name}: {exc}")
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

    Angel One uses YYMON format (no day) for ALL option symbols —
    both weekly and monthly. The distinction only affects which
    contract month is selected, not the string format.

    Weekly instruments  → nearest upcoming expiry_weekday month
                          format: YYMON  e.g. "25SEP"  → NIFTY25SEP23350CE
    Monthly instruments → last occurrence of expiry_weekday in the
                          current contract month (rolls to next month
                          if today is past that date)
                          format: YYMON  e.g. "25JUL"

    Current schedule (2025-26):
      NIFTY     → Tuesday  weekly   → "26SEP"
      SENSEX    → Thursday weekly   → "26SEP"
      BANKNIFTY → last Tue monthly  → "26SEP"
      BANKEX    → last Thu monthly  → "26SEP"
      FINNIFTY  → last Tue monthly  → "26SEP"
    """
    inst = get_instrument(instrument_name)
    now  = datetime.now()

    if inst.expiry_type == "weekly":
        target_dow = inst.expiry_weekday
        days_ahead = (target_dow - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7          # already on expiry day → use next week's
        expiry_dt = now + timedelta(days=days_ahead)
        return expiry_dt.strftime("%y%b").upper()   # YYMON only — no day component

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
        with _API_LOCK:
            _api_throttle()
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
