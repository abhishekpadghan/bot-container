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
import json
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
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECS", "60"))
MIN_CONFIDENCE   = int(os.getenv("MIN_SIGNAL_CONFIDENCE", "6"))

# ── Global state — one trader + risk engine per instrument ────
# Each instrument runs independently: its own trade, its own risk state.
_traders:      dict[str, PaperTrader] = {}
_risks:        dict[str, RiskEngine]  = {}
_highest_ltps: dict[str, float]       = {}
_running = True

# ── Live state shared with dashboard ──────────────────────────
# Written to data/live_state.json on every tick.
# Dashboard reads this file via /live API endpoint — no IPC needed.
_live_state: dict = {
    "timestamp": "",
    "vix":       0.0,
    "regime":    "UNKNOWN",
    "instruments": {},   # keyed by inst_name
}
_LIVE_STATE_PATH = os.path.join(config.DATA_DIR, "live_state.json")


def _write_live_state() -> None:
    """Atomically write _live_state to disk for the dashboard to read."""
    try:
        tmp = _LIVE_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_live_state, f)
        os.replace(tmp, _LIVE_STATE_PATH)   # atomic on POSIX
    except Exception as exc:
        logger.debug(f"live_state write error: {exc}")

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
        _live_state["timestamp"] = datetime.now().isoformat()
        _live_state["regime"]    = "WEEKEND"
        _write_live_state()
        logger.debug("Weekend — sleeping 1h")
        time.sleep(3600)
        return

    if not _is_market_open():
        _live_state["timestamp"] = datetime.now().isoformat()
        _live_state["regime"]    = "CLOSED"
        _write_live_state()
        logger.debug("Outside market hours — waiting 60s")
        time.sleep(60)
        return

    sm  = SessionManager.get()

    # VIX is fetched AFTER the per-instrument candle calls below.
    # Fetching VIX first (ltpData) consumes the throttle gap and pushes
    # getCandleData into Angel One's rate-limit window.
    # We set a default here and overwrite it after instruments are processed.
    vix = 0.0

    _live_state["timestamp"] = datetime.now().isoformat()
    _live_state["vix"]       = 0.0

    # Shared blackout check (applies to all instruments)
    blackout, reason = list(_risks.values())[0].is_blackout_day()
    if blackout:
        _live_state["regime"] = f"BLACKOUT: {reason}"
        _write_live_state()
        logger.info(f"🚫 {reason}")
        time.sleep(300)
        return

    # ── Process each instrument independently ─────────────────
    # The global _API_LOCK + per-call delay in market_data already serialises
    # all ltpData calls — no extra time.sleep() stagger needed here.
    # Removing the 3s sleep reduces per-tick latency so get_spot() fires
    # sooner and the displayed index value is more current.
    for inst_name in config.INSTRUMENTS:
        try:
            _tick_instrument(inst_name, vix, sm)
        except Exception as exc:
            logger.error(f"[{inst_name}] Tick error: {exc}", exc_info=True)

    # Fetch VIX AFTER all candle/spot calls so it doesn't burn the throttle
    # gap that getCandleData needs. VIX=0 means no VIX filter this tick —
    # acceptable trade-off to avoid rate-limiting the candle fetch.
    vix = md.get_india_vix()
    _live_state["vix"] = round(vix, 2)

    _write_live_state()


def _tick_instrument(inst_name: str, vix: float, sm) -> None:
    """Process one instrument per tick — fetch data, check exit, check entry."""

    trader  = _traders[inst_name]
    risk    = _risks[inst_name]
    inst    = md.get_instrument(inst_name)

    # Initialise this instrument's live state slot
    istate = _live_state["instruments"].setdefault(inst_name, {
        "spot": 0.0, "status": "FLAT", "ltp": 0.0,
        "entry_price": 0.0, "floating_pnl": 0.0, "floating_pnl_inr": 0.0,
        "symbol": "", "option_type": "", "strike": 0,
        "regime": "—", "strategy": "—", "confidence": 0, "signal_reason": "—",
        "daily_pnl": 0.0, "trade_count": 0,
        "supertrend": "—", "rsi": 0.0, "ema_cross": "—", "vwap_pos": "—",
        "pcr": 0.0, "max_pain": 0,
    })

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
            _highest_ltps[inst_name] = 0.0
        istate["status"] = "EOD"
        return

    # ── Fetch candles + spot ───────────────────────────────────
    # Only fetch 5-min candles every tick (indicators + regime detection).
    # 1-min candles are fetched lazily only when a signal is being evaluated
    # (entry path below) to keep the per-tick API call count low.
    try:
        df_5min = md.get_index_candles(inst_name, interval="FIVE_MINUTE")
        spot    = md.get_spot(inst_name)
    except Exception as exc:
        logger.error(f"[{inst_name}] Market data error: {exc}")
        istate["status"] = "DATA_ERR"
        return

    istate["spot"] = round(spot, 2)

    # ── Fetch option chain ────────────────────────────────────
    option_chain = None
    try:
        expiry_str   = md.get_expiry_string(inst_name)
        option_chain = fetch_option_chain(sm.smart(), spot, expiry_str)
        if option_chain:
            istate["pcr"]      = round(option_chain.get("pcr", 0.0), 2)
            istate["max_pain"] = int(option_chain.get("max_pain", 0))
    except Exception as exc:
        logger.debug(f"[{inst_name}] Option chain unavailable: {exc}")

    # ── Compute indicators for signal panel ───────────────────
    try:
        from strategy import compute_supertrend, compute_ema, compute_rsi, compute_vwap
        import numpy as _np
        _cl = df_5min["close"]
        _st = compute_supertrend(df_5min)
        _ema_fast = _cl.ewm(span=9,  adjust=False).mean()
        _ema_slow = _cl.ewm(span=21, adjust=False).mean()
        _rsi      = compute_rsi(_cl)
        _vwap     = compute_vwap(df_5min)
        istate["supertrend"] = "UP"   if _st.iloc[-1] else "DOWN"
        istate["ema_cross"]  = "BULL" if _ema_fast.iloc[-1] > _ema_slow.iloc[-1] else "BEAR"
        istate["rsi"]        = round(float(_rsi.iloc[-1]), 1)
        istate["vwap_pos"]   = "ABOVE" if _cl.iloc[-1] > _vwap.iloc[-1] else "BELOW"
    except Exception:
        pass

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
        entry        = trader.open_trade["entry_price"]
        floating_pts = ltp - entry
        float_inr    = floating_pts * inst.lot_size

        # Update live state with floating P&L
        istate.update({
            "status":          "IN_TRADE",
            "ltp":             round(ltp, 2),
            "entry_price":     round(entry, 2),
            "floating_pnl":    round(floating_pts, 2),
            "floating_pnl_inr": round(float_inr, 2),
            "symbol":          trader.open_trade["symbol"],
            "option_type":     trader.open_trade["option_type"],
            "strike":          trader.open_trade["strike"],
            "daily_pnl":       round(trader.daily_pnl, 2),
            "trade_count":     trader.trade_count,
        })

        should_exit, exit_reason = risk.trailing_stop(
            entry, ltp, _highest_ltps[inst_name]
        )
        if should_exit:
            trader.exit(ltp, exit_reason)
            risk.record_trade(ltp - entry)
            _highest_ltps[inst_name] = 0.0
            istate["status"] = "FLAT"
        return

    # ── ENTRY logic ───────────────────────────────────────────
    istate.update({
        "status":          "FLAT",
        "ltp":             0.0,
        "floating_pnl":    0.0,
        "floating_pnl_inr": 0.0,
        "daily_pnl":       round(trader.daily_pnl, 2),
        "trade_count":     trader.trade_count,
    })

    if not _is_entry_window():
        return

    can_trade, block_reason = risk.can_trade()
    if not can_trade:
        logger.info(f"[{inst_name}] 🔒 {block_reason}")
        istate["regime"] = f"BLOCKED: {block_reason}"
        return

    # Use 5-min candles for entry evaluation as well.
    # Fetching 1-min candles here would fire a second getCandleData call
    # in the same tick — Angel One allows only ~1 historical call/min per key,
    # so this would immediately trigger AB1021. The 5-min data is already
    # cached and carries the same signal quality at a 30–60s poll interval.
    df_1min = df_5min

    signal, regime = select_strategy(
        df_5min=df_5min, df_1min=df_1min,
        vix=vix, option_chain=option_chain, spot=spot,
    )

    # Always write regime + signal to live state (even if no trade)
    _live_state["regime"] = regime or "—"
    istate["regime"] = regime or "—"
    if signal:
        istate["strategy"]      = signal.strategy or "—"
        istate["confidence"]    = signal.confidence
        istate["signal_reason"] = (signal.reason or "")[:80]

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

    # ── Step 1: Login only (no heartbeat yet) ─────────────────────────
    # The heartbeat thread makes its own ltpData ping every 60s.
    # Starting it before warmup means it races the first tick's API calls.
    # We start it explicitly after the full warmup sequence below.
    try:
        SessionManager.get().connect()
    except Exception as exc:
        logger.critical(f"❌ Login failed: {exc}")
        sys.exit(1)

    # ── Step 2: Start heartbeat (it sleeps 60s before first ping) ─────
    # Heartbeat first actual API call is at T+60s — safe to start now.
    SessionManager.get().start_heartbeat()

    # ── Step 3: Warmup ────────────────────────────────────────────────
    # generateSession counts against Angel One's historical-data quota.
    # Empirically their rolling window exceeds 60s — the first getCandleData
    # call at T=66s still hits the rate limit. Using 75s clears it reliably.
    md._vix_token_resolved = ""   # mark VIX as resolved (disabled) — skip probe
    # Prime the throttle timestamp so the first getCandleData call doesn't
    # fire until at least _CANDLE_CALL_DELAY seconds AFTER this warmup ends.
    import time as _t
    md._last_api_call_t = _t.monotonic()   # throttle starts counting from now
    logger.info("⏳ Waiting 75s for Angel One rate-limit window to clear...")
    time.sleep(75)
    logger.info("✅ Session ready — starting market loop")

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
