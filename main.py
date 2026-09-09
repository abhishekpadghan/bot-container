"""
main.py — NiftyBot entry point.

Main loop flow:
  1. Login to Angel One SmartAPI
  2. Every POLL_INTERVAL seconds (default 30s):
     a. Check if within trading hours
     b. Fetch live Nifty candles + India VIX
     c. If FLAT: run strategy → enter if signal is BUY
     d. If IN_TRADE: check exit conditions → exit if triggered
     e. Force-exit all positions at EXIT_ALL_TIME
  3. Log out cleanly on shutdown
"""

import time
import signal
import sys
from datetime import datetime, time as dtime
from loguru import logger

import config
import auth
import market_data as md
from strategy import generate_signal, should_exit, Signal
from paper_trader import PaperTrader

# ── Logging setup ─────────────────────────────────────────────
# Logs go to stdout — captured by `podman logs` / `./run.sh logs`.
# File logging is optional and only enabled if /app/logs is writable.
import os
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="INFO",
    colorize=True,
)
_log_file = os.path.join(config.LOG_DIR, "bot_{time:YYYY-MM-DD}.log")
try:
    logger.add(
        _log_file,
        rotation="00:00",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )
except Exception:
    logger.warning("File logging unavailable — stdout only (volume permission issue)")

# ── Constants ─────────────────────────────────────────────────
POLL_INTERVAL  = 30      # seconds between each market data check
CANDLE_INTERVAL = "FIVE_MINUTE"

_trader = PaperTrader()
_running = True

# ── Graceful shutdown on SIGINT / SIGTERM ─────────────────────

def _shutdown_handler(sig, frame):
    global _running
    logger.warning("⚡ Shutdown signal received — cleaning up...")
    _running = False

signal.signal(signal.SIGINT,  _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


# ── Time helpers ──────────────────────────────────────────────

def _parse_time(t: str) -> dtime:
    h, m = map(int, t.split(":"))
    return dtime(h, m)


def _is_market_open() -> bool:
    """True if current IST time is within trading window."""
    now = datetime.now().time()
    return _parse_time("09:15") <= now <= _parse_time("15:30")


def _is_entry_window() -> bool:
    """True if new entries are allowed right now."""
    now = datetime.now().time()
    return _parse_time(config.ENTRY_TIME_START) <= now <= _parse_time(config.ENTRY_TIME_END)


def _is_force_exit_time() -> bool:
    """True if we've passed the EOD force-exit time."""
    return datetime.now().time() >= _parse_time(config.EXIT_ALL_TIME)


def _is_weekend() -> bool:
    """True on Saturday (5) and Sunday (6) — NSE is closed."""
    return datetime.now().weekday() >= 5


# ── Expiry resolution ─────────────────────────────────────────

def _get_expiry_string() -> str:
    """
    Build the expiry string for the option symbol.
    Weekly example: "23SEP07"  (YY + Mon + DD)
    Monthly example: "23SEP"   (YY + Mon)
    Angel One uses uppercase month abbreviation.
    """
    now = datetime.now()
    if config.EXPIRY_TYPE == "weekly":
        # Next Thursday is weekly expiry
        days_ahead = (3 - now.weekday()) % 7  # 3 = Thursday
        if days_ahead == 0:
            days_ahead = 7
        from datetime import timedelta
        expiry_dt = now + timedelta(days=days_ahead)
        return expiry_dt.strftime("%y%b%d").upper()
    else:
        return now.strftime("%y%b").upper()


# ── Main trading loop ─────────────────────────────────────────

def run() -> None:
    global _running

    logger.info("=" * 60)
    logger.info(f"  NiftyBot starting | Mode: {config.TRADING_MODE.upper()}")
    logger.info(f"  Instrument: {config.INSTRUMENT} | Lot: {config.LOT_SIZE}")
    logger.info(f"  Target: +{config.PROFIT_TARGET_POINTS} pts / SL: -{config.STOP_LOSS_POINTS} pts")
    logger.info(f"  Entry window: {config.ENTRY_TIME_START} – {config.ENTRY_TIME_END} IST")
    logger.info(f"  Force exit: {config.EXIT_ALL_TIME} IST")
    logger.info("=" * 60)

    # ── Login ─────────────────────────────────────────────────
    try:
        auth.login()
    except Exception as exc:
        logger.critical(f"❌ Login failed: {exc}")
        sys.exit(1)

    while _running:
        try:
            _tick()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(f"Tick error: {exc}", exc_info=True)

        if not _running:
            break

        time.sleep(POLL_INTERVAL)

    # ── Cleanup ───────────────────────────────────────────────
    if not _trader.is_flat:
        logger.warning("🔔 Shutdown with open trade — force-exiting...")
        try:
            ltp = md.get_ltp(
                _trader.open_trade["symbol"],
                "",   # token unavailable here; acceptable for shutdown exit
                exchange="NFO"
            )
        except Exception:
            ltp = _trader.open_trade["entry_price"]
        _trader.exit(ltp, "SHUTDOWN")

    auth.logout()
    logger.info("👋 NiftyBot stopped.")


def _tick() -> None:
    """Single iteration of the main loop."""

    # ── Weekend / non-market guard ────────────────────────────
    if _is_weekend():
        logger.debug("Weekend — market closed. Sleeping...")
        time.sleep(3600)
        return

    if not _is_market_open():
        logger.debug("Outside market hours. Waiting...")
        time.sleep(60)
        return

    # ── Force exit check (EOD) ────────────────────────────────
    if _is_force_exit_time() and not _trader.is_flat:
        logger.warning(f"⏰ {config.EXIT_ALL_TIME} reached — force-closing position")
        try:
            ltp = md.get_ltp(
                _trader.open_trade["symbol"],
                md.get_symbol_token(
                    _trader.open_trade["symbol"], exchange="NFO"
                ),
                exchange="NFO"
            )
        except Exception as e:
            logger.error(f"LTP fetch failed for force exit: {e}")
            return
        _trader.exit(ltp, "EOD_FORCE_EXIT")
        return

    if _is_force_exit_time():
        logger.debug("Past force-exit time, no open position. Done for today.")
        time.sleep(60)
        return

    # ── Fetch Nifty candles & VIX ─────────────────────────────
    try:
        df  = md.get_nifty_candles(interval=CANDLE_INTERVAL)
        vix = md.get_india_vix()
    except Exception as exc:
        logger.error(f"Market data fetch error: {exc}")
        return

    # ── If flat and in entry window → evaluate entry ──────────
    if _trader.is_flat and _is_entry_window() and _trader.can_trade:
        result = generate_signal(df, vix=vix)

        if result.signal == Signal.BUY and result.option_type is not None:
            # Resolve strike and symbol
            try:
                spot   = md.get_nifty_spot()
                strike = md.get_atm_strike(spot)
                expiry = _get_expiry_string()
                symbol = md.build_option_symbol(
                    config.INSTRUMENT, expiry, strike, result.option_type.value
                )
                token = md.get_symbol_token(symbol, exchange="NFO")
                ltp   = md.get_ltp(symbol, token, exchange="NFO")
            except Exception as exc:
                logger.error(f"Entry symbol resolution error: {exc}")
                return

            _trader.enter(
                symbol=symbol,
                strike=strike,
                option_type=result.option_type.value,
                ltp=ltp,
                confidence=result.confidence,
                signal_detail=result.reason,
            )
        else:
            logger.debug(f"Signal: HOLD — {result.reason}")

    # ── If in trade → check exit ──────────────────────────────
    elif not _trader.is_flat:
        try:
            symbol = _trader.open_trade["symbol"]
            token  = md.get_symbol_token(symbol, exchange="NFO")
            ltp    = md.get_ltp(symbol, token, exchange="NFO")
        except Exception as exc:
            logger.error(f"LTP fetch for exit check failed: {exc}")
            return

        exit_now, reason = should_exit(_trader.open_trade["entry_price"], ltp, df)
        if exit_now:
            _trader.exit(ltp, reason)


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    run()
