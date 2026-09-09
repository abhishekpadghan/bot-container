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

# ── Global state — one trader + risk engine per instrument ────
# Each instrument runs independently: its own trade, its own risk state.
_traders:      dict[str, PaperTrader] = {}
_risks:        dict[str, RiskEngine]  = {}
_highest_ltps: dict[str, float]       = {}
_running = True

for _inst in config.INSTRUMENTS:
    _traders[_inst]      = PaperTrader()
    _risks[_inst]        = RiskEngine()
    _highest_ltps[_inst] = 0.0

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

# ── Per-tick logic ────────────────────────────────────────────

def _tick():
    """One iteration of the main loop — processes ALL configured instruments."""

    if _is_weekend():
        logger.debug("Weekend — sleeping 1h")
        time.sleep(3600)
        return

    if not _is_market_open():
        logger.debug("Outside market hours — waiting 60s")
        time.sleep(60)
        return

    sm  = SessionManager.get()
    vix = md.get_india_vix()

    # Shared blackout check (applies to all instruments)
    blackout, reason = list(_risks.values())[0].is_blackout_day()
    if blackout:
        logger.info(f"🚫 {reason}")
        time.sleep(300)
        return

    # ── Process each instrument independently ─────────────────
    for inst_name in config.INSTRUMENTS:
        try:
            _tick_instrument(inst_name, vix, sm)
        except Exception as exc:
            logger.error(f"[{inst_name}] Tick error: {exc}", exc_info=True)


def _tick_instrument(inst_name: str, vix: float, sm) -> None:
    """Process one instrument per tick — fetch data, check exit, check entry."""

    trader  = _traders[inst_name]
    risk    = _risks[inst_name]
    inst    = md.get_instrument(inst_name)

    # ── Force-exit at EOD ─────────────────────────────────────
    if _is_force_exit_time():
        if not trader.is_flat:
            logger.warning(f"[{inst_name}] ⏰ EOD force-close")
            try:
                sym   = trader.open_trade["symbol"]
                token = trader.open_trade.get("symbol_token", "")
                fo_ex = inst.fo_exchange
                ltp   = md.get_ltp(sym, token, exchange=fo_ex)
            except Exception:
                ltp = trader.open_trade["entry_price"]
            trader.exit(ltp, "EOD_FORCE_EXIT")
            risk.record_trade(ltp - trader.open_trade.get("entry_price", ltp) if trader.open_trade else 0)
        return

    # ── Fetch candles + spot ───────────────────────────────────
    try:
        df_5min = md.get_index_candles(inst_name, interval="FIVE_MINUTE")
        df_1min = md.get_index_candles(inst_name, interval="ONE_MINUTE", lookback_days=1)
        spot    = md.get_spot(inst_name)
    except Exception as exc:
        logger.error(f"[{inst_name}] Market data error: {exc}")
        return

    # ── Fetch option chain ────────────────────────────────────
    option_chain = None
    try:
        expiry_str   = md.get_expiry_string(inst_name)
        option_chain = fetch_option_chain(sm.smart(), spot, expiry_str)
    except Exception as exc:
        logger.debug(f"[{inst_name}] Option chain unavailable: {exc}")

    # ── EXIT logic ────────────────────────────────────────────
    if not trader.is_flat:
        try:
            sym   = trader.open_trade["symbol"]
            token = trader.open_trade.get("symbol_token", "")
            ltp   = md.get_ltp(sym, token, exchange=inst.fo_exchange)
        except Exception as exc:
            logger.error(f"[{inst_name}] LTP fetch error: {exc}")
            return

        _highest_ltps[inst_name] = max(_highest_ltps[inst_name], ltp)
        entry = trader.open_trade["entry_price"]
        should_exit, exit_reason = risk.trailing_stop(
            entry, ltp, _highest_ltps[inst_name]
        )
        if should_exit:
            trader.exit(ltp, exit_reason)
            risk.record_trade(ltp - entry)
            _highest_ltps[inst_name] = 0.0
        return

    # ── ENTRY logic ───────────────────────────────────────────
    if not _is_entry_window():
        return

    can_trade, block_reason = risk.can_trade()
    if not can_trade:
        logger.info(f"[{inst_name}] 🔒 {block_reason}")
        return

    signal, regime = select_strategy(
        df_5min=df_5min, df_1min=df_1min,
        vix=vix, option_chain=option_chain, spot=spot,
    )

    if signal is None or signal.signal.value != "BUY":
        logger.debug(f"[{inst_name}] No signal | regime={regime}")
        return

    if signal.confidence < MIN_CONFIDENCE:
        logger.debug(f"[{inst_name}] Confidence {signal.confidence} < {MIN_CONFIDENCE}")
        return

    # Resolve symbol, token, LTP
    try:
        expiry_str = md.get_expiry_string(inst_name)
        strike     = md.get_atm_strike(spot, strike_gap=inst.strike_gap)
        opt_type   = signal.option_type.value if signal.option_type else "CE"
        symbol     = md.build_option_symbol(inst_name, expiry_str, strike, opt_type)
        token      = md.get_symbol_token(symbol, exchange=inst.fo_exchange)
        ltp        = md.get_ltp(symbol, token, exchange=inst.fo_exchange)
    except Exception as exc:
        logger.error(f"[{inst_name}] Symbol resolution error: {exc}")
        return

    # Liquidity check
    liquid, liq_reason = md.check_liquidity(symbol, token, exchange=inst.fo_exchange)
    if not liquid:
        logger.warning(f"[{inst_name}] 🚫 {liq_reason}")
        return

    trader.enter(
        symbol=symbol, strike=strike, option_type=opt_type, ltp=ltp,
        symbol_token=token, confidence=signal.confidence,
        signal_detail=f"{inst_name}|{signal.strategy}|{signal.reason[:60]}",
    )
    _highest_ltps[inst_name] = ltp

# ── Startup validation ────────────────────────────────────────

_EXPIRY_DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday",
                     3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}

def _print_startup_banner() -> None:
    """
    Print a clearly readable startup validation table so the operator
    can visually confirm every instrument's registry values before the
    bot begins trading.  Raises SystemExit if any instrument is unknown.
    """
    logger.info("=" * 72)
    logger.info(f"  NiftyBot v2  |  Mode: {config.TRADING_MODE.upper()}")
    logger.info("=" * 72)
    logger.info(f"  {'INSTRUMENT':<12} {'LOT':>5}  {'STRIKE_GAP':>10}  "
                f"{'EXPIRY_DAY':<12}  {'INDEX_EXCH':<6}  {'F&O_EXCH':<6}")
    logger.info(f"  {'-'*12} {'-'*5}  {'-'*10}  {'-'*12}  {'-'*6}  {'-'*6}")

    for inst_name in config.INSTRUMENTS:
        try:
            inst = md.get_instrument(inst_name)
        except ValueError as exc:
            logger.critical(f"❌ Config error: {exc}")
            sys.exit(1)

        expiry_day  = _EXPIRY_DAY_NAMES.get(inst.expiry_weekday, "?")
        expiry_str  = md.get_expiry_string(inst_name)
        lot_src     = f"{inst.lot_size} (env override)" \
                      if f"{inst_name}_LOT_SIZE" in os.environ \
                      else f"{inst.lot_size} (registry default)"
        logger.info(
            f"  {inst_name:<12} {inst.lot_size:>5}  {inst.strike_gap:>10}  "
            f"{expiry_day:<12}  {inst.index_exchange:<6}  {inst.fo_exchange:<6}"
            f"  → expiry={expiry_str}  lot={lot_src}"
        )

    logger.info("=" * 72)
    logger.info(f"  Entry window : {config.ENTRY_TIME_START} – {config.ENTRY_TIME_END} IST")
    logger.info(f"  Force exit   : {config.EXIT_ALL_TIME} IST")
    logger.info(f"  Target/SL    : +{config.PROFIT_TARGET_POINTS} / -{config.STOP_LOSS_POINTS} pts")
    logger.info(f"  Daily target : ₹{config.DAILY_PROFIT_TARGET} / Max loss: ₹{config.DAILY_MAX_LOSS}")
    logger.info(f"  Poll interval: {POLL_INTERVAL}s  |  Min confidence: {MIN_CONFIDENCE}/10")
    logger.info("=" * 72)
    if config.TRADING_MODE == "live":
        logger.warning("⚠️  LIVE MODE — REAL ORDERS WILL BE PLACED.  Ctrl+C to abort now.")
        time.sleep(5)   # 5-second window to abort before first tick


# ── Main entry ────────────────────────────────────────────────

def run():
    _print_startup_banner()

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

    # ── Cleanup — exit any open positions on all instruments ──
    for inst_name, trader in _traders.items():
        if not trader.is_flat:
            logger.warning(f"[{inst_name}] Open trade on shutdown — force-exiting")
            entry = trader.open_trade["entry_price"]
            trader.exit(entry, "SHUTDOWN")

    SessionManager.get().disconnect()
    logger.info("👋 NiftyBot stopped.")


if __name__ == "__main__":
    run()
