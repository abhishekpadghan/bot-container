"""
main.py — NiftyBot orchestrator (Production-grade).

Full loop:
  1. Login via SessionManager (auto-reconnect, heartbeat)
  2. Every POLL_INTERVAL seconds:
     a. Check market hours + risk gates + blackout dates
     b. Fetch 5-min + 1-min Nifty candles + India VIX
     c. Fetch option chain (PCR, OI, Greeks, IV Rank)
     d. Strategy selector → picks best strategy for conditions
     e. If FLAT: enter if signal confidence >= threshold
     f. If IN_TRADE: trailing SL + target + signal flip exit
     g. Force-exit at EXIT_ALL_TIME
  3. Graceful shutdown on SIGINT/SIGTERM
"""

import os
import sys
import time
import signal
import threading
from datetime import datetime, time as dtime
from loguru import logger

import config
from session_manager import SessionManager
import market_data as md
from risk_engine import RiskEngine
from paper_trader import PaperTrader, get_daily_summary
from strategy_selector import select_strategy
from signals_advanced import fetch_option_chain

# ── Logging ───────────────────────────────────────────────────
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
    logger.warning("File logging unavailable — stdout only")

# ── Constants ─────────────────────────────────────────────────
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECS", "30"))
MIN_CONFIDENCE   = int(os.getenv("MIN_SIGNAL_CONFIDENCE", "6"))

# ── Global state ──────────────────────────────────────────────
_trader  = PaperTrader()
_risk    = RiskEngine()
_running = True
_highest_ltp: float = 0.0   # tracks highest LTP for trailing SL

# ── Shutdown handler ──────────────────────────────────────────

def _shutdown(sig, frame):
    global _running
    logger.warning("⚡ Shutdown signal — cleaning up...")
    _running = False

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ── Time helpers ──────────────────────────────────────────────

def _t(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)

def _now() -> dtime:
    return datetime.now().time()

def _is_market_open() -> bool:
    return _t("09:15") <= _now() <= _t("15:30")

def _is_entry_window() -> bool:
    return _t(config.ENTRY_TIME_START) <= _now() <= _t(config.ENTRY_TIME_END)

def _is_force_exit_time() -> bool:
    return _now() >= _t(config.EXIT_ALL_TIME)

def _is_weekend() -> bool:
    return datetime.now().weekday() >= 5

# ── Expiry builder ────────────────────────────────────────────

def _get_expiry_string() -> str:
    from datetime import timedelta, date
    now = datetime.now()
    if config.EXPIRY_TYPE == "weekly":
        days_ahead = (3 - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        expiry_dt = now + timedelta(days=days_ahead)
        return expiry_dt.strftime("%y%b%d").upper()
    return now.strftime("%y%b").upper()

# ── Per-tick logic ────────────────────────────────────────────

def _tick():
    global _highest_ltp

    # Weekend / pre-market guard
    if _is_weekend():
        logger.debug("Weekend — sleeping 1h")
        time.sleep(3600)
        return

    if not _is_market_open():
        logger.debug("Outside market hours — waiting 60s")
        time.sleep(60)
        return

    sm = SessionManager.get()

    # ── Force-exit at EOD ─────────────────────────────────────
    if _is_force_exit_time():
        if not _trader.is_flat:
            logger.warning(f"⏰ {config.EXIT_ALL_TIME} — force-closing position")
            try:
                sym   = _trader.open_trade["symbol"]
                token = md.get_symbol_token(sym, exchange="NFO")
                ltp   = md.get_ltp(sym, token, exchange="NFO")
            except Exception:
                ltp = _trader.open_trade["entry_price"]
            _trader.exit(ltp, "EOD_FORCE_EXIT")
            _risk.record_trade((ltp - _trader.open_trade.get("entry_price", ltp)) if _trader.open_trade else 0)
        time.sleep(60)
        return

    # ── Risk gate check ───────────────────────────────────────
    blackout, reason = _risk.is_blackout_day()
    if blackout:
        logger.info(f"🚫 {reason}")
        time.sleep(300)
        return

    # ── Fetch market data ─────────────────────────────────────
    try:
        df_5min  = md.get_nifty_candles(interval="FIVE_MINUTE")
        df_1min  = md.get_nifty_candles(interval="ONE_MINUTE", lookback_days=1)
        vix      = md.get_india_vix()
        spot     = md.get_nifty_spot()
    except Exception as exc:
        logger.error(f"Market data error: {exc}")
        return

    # ── Fetch option chain (PCR / OI / Greeks) ────────────────
    option_chain = None
    try:
        expiry_str = _get_expiry_string()
        strike     = md.get_atm_strike(spot)
        symbol_ce  = md.build_option_symbol(config.INSTRUMENT, expiry_str, strike, "CE")
        option_chain = fetch_option_chain(sm.smart(), spot, expiry_str)
    except Exception as exc:
        logger.debug(f"Option chain unavailable: {exc} — proceeding without")

    # ── EXIT logic (if in trade) ──────────────────────────────
    if not _trader.is_flat:
        try:
            sym   = _trader.open_trade["symbol"]
            token = md.get_symbol_token(sym, exchange="NFO")
            ltp   = md.get_ltp(sym, token, exchange="NFO")
        except Exception as exc:
            logger.error(f"LTP fetch error: {exc}")
            return

        _highest_ltp = max(_highest_ltp, ltp)
        entry        = _trader.open_trade["entry_price"]

        # Use trailing SL from risk engine
        should_exit, exit_reason = _risk.trailing_stop(entry, ltp, _highest_ltp)

        if should_exit:
            _trader.exit(ltp, exit_reason)
            _risk.record_trade(ltp - entry)
            _highest_ltp = 0.0
        return

    # ── ENTRY logic (if flat) ─────────────────────────────────
    if not _is_entry_window():
        logger.debug("Outside entry window")
        return

    can_trade, block_reason = _risk.can_trade()
    if not can_trade:
        logger.info(f"🔒 Trade blocked: {block_reason}")
        return

    # Strategy selector picks best strategy
    signal, regime = select_strategy(
        df_5min=df_5min,
        df_1min=df_1min,
        vix=vix,
        option_chain=option_chain,
        spot=spot,
    )

    if signal is None or signal.signal.value != "BUY":
        logger.debug(f"No entry signal | regime={regime}")
        return

    if signal.confidence < MIN_CONFIDENCE:
        logger.debug(f"Signal confidence {signal.confidence} < {MIN_CONFIDENCE} — skipping")
        return

    # Resolve option symbol, fetch token + LTP
    try:
        expiry_str = _get_expiry_string()
        strike     = md.get_atm_strike(spot)
        opt_type   = signal.option_type.value if signal.option_type else "CE"
        symbol     = md.build_option_symbol(config.INSTRUMENT, expiry_str, strike, opt_type)
        token      = md.get_symbol_token(symbol, exchange="NFO")
        ltp        = md.get_ltp(symbol, token, exchange="NFO")
    except Exception as exc:
        logger.error(f"Entry symbol resolution error: {exc}")
        return

    # BLOCKER 5 FIX: OI liquidity check — skip illiquid strikes
    liquid, liq_reason = md.check_liquidity(symbol, token, exchange="NFO")
    if not liquid:
        logger.warning(f"🚫 Skipping entry — {liq_reason}")
        return

    # Position sizing
    lots = _risk.position_size(method=os.getenv("SIZING_METHOD", "fixed_fractional"))

    _trader.enter(
        symbol=symbol,
        strike=strike,
        option_type=opt_type,
        ltp=ltp,
        symbol_token=token,          # BLOCKER 1 FIX: pass token through
        confidence=signal.confidence,
        signal_detail=f"{signal.strategy}|{signal.reason[:80]}",
    )
    _highest_ltp = ltp   # reset trailing tracker

# ── Main entry ────────────────────────────────────────────────

def run():
    logger.info("=" * 60)
    logger.info(f"  NiftyBot v2 | Mode: {config.TRADING_MODE.upper()}")
    logger.info(f"  Instrument : {config.INSTRUMENT} | Lot: {config.LOT_SIZE}")
    logger.info(f"  Target     : +{config.PROFIT_TARGET_POINTS}pts / SL: -{config.STOP_LOSS_POINTS}pts")
    logger.info(f"  Entry      : {config.ENTRY_TIME_START}–{config.ENTRY_TIME_END} IST")
    logger.info(f"  Force exit : {config.EXIT_ALL_TIME} IST")
    logger.info(f"  Poll every : {POLL_INTERVAL}s")
    logger.info("=" * 60)

    try:
        SessionManager.get().connect()
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
        logger.warning("Open trade on shutdown — force-exiting")
        entry = _trader.open_trade["entry_price"]
        _trader.exit(entry, "SHUTDOWN")

    SessionManager.get().disconnect()
    logger.info("👋 NiftyBot stopped.")


if __name__ == "__main__":
    run()
