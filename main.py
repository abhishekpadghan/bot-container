"""
main.py — NiftyBot orchestrator.

Timing model (candle-aligned):
  * Entry decisions are made ONCE per CLOSED 5-minute candle, a few seconds after
    the candle closes (09:20:06, 09:25:06, …) using completed candles only —
    never the half-formed current candle.
  * While a position is open, the stop / target / trailing stop are checked every
    EXIT_POLL_SECS (default 4s) against the option's live price, using the exit
    rules saved WITH that trade (so they survive a restart).
  * A background thread refreshes the index spot every LIVE_SPOT_INTERVAL seconds
    so the dashboard moves in real time.

Restart safety:
  * Today's P&L, trade count, loss streak and any OPEN position are rebuilt from
    data/trades.db on startup. Shutting the bot down never force-closes a position.
  * Full trade + order history is exported to data/exports/*.csv and snapshotted
    daily, in the persistent data volume.
"""

import os
import sys
import time
import json
import signal
import threading
from datetime import datetime, timedelta, time as dtime
from loguru import logger
import pandas as pd

import config
from session_manager import SessionManager
import market_data as md
from risk_engine import RiskEngine
from paper_trader import PaperTrader, export_history
from candle_store import CandleStore
from strategy_selector import select_strategy
from strategies import ALL_STRATEGIES, ENABLED_STRATEGIES
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
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECS", "60"))     # max idle sleep between ticks
MIN_CONFIDENCE   = int(os.getenv("MIN_SIGNAL_CONFIDENCE", "6"))
LIVE_SPOT_INTERVAL = int(os.getenv("LIVE_SPOT_INTERVAL_SECS", "5"))   # dashboard spot refresh; 0 = off
VIX_REFRESH_SECS = 60
BAR_MIN          = config.CANDLE_MINUTES
CANDLE_INTERVAL  = "FIVE_MINUTE"
MAX_CANDLE_WAITS = 8      # ticks to wait for a just-closed candle to be published before using what we have

# ── Global state — one trader + risk engine per instrument ────
# Each instrument runs independently: its own trade, its own risk state.
_traders:      dict[str, PaperTrader] = {}
_risks:        dict[str, RiskEngine]  = {}
_stores:       dict[str, CandleStore] = {}    # local-first candles (see candle_store.py)
_last_eval_bar: dict[str, datetime]   = {}    # last closed candle each instrument was evaluated on
_candle_waits:  dict[str, int]        = {}
_retry_soon    = False                         # set when a tick is waiting on data
_last_vix_t    = 0.0
_running       = True

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


_write_lock = threading.Lock()   # main loop and spot-stream thread both write the file


def _write_live_state() -> None:
    """Atomically write _live_state to disk for the dashboard to read."""
    try:
        with _write_lock:
            tmp = _LIVE_STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_live_state, f)
            os.replace(tmp, _LIVE_STATE_PATH)   # atomic on POSIX
    except Exception as exc:
        logger.debug(f"live_state write error: {exc}")


def _spot_stream_loop() -> None:
    """
    Refresh index spot prices every LIVE_SPOT_INTERVAL seconds so the
    dashboard moves between strategy ticks.
    Only touches display fields — trading decisions stay in _tick().
    """
    while _running:
        _sleep(LIVE_SPOT_INTERVAL)
        if _is_weekend() or not _is_market_open():
            continue
        updated = False
        for inst_name in config.INSTRUMENTS:
            istate = _live_state["instruments"].get(inst_name)
            if istate is None:          # main loop hasn't initialised it yet
                continue
            try:
                spot = md.get_spot(inst_name)
                istate["spot"] = round(spot, 2)
                _stores[inst_name].add_tick(datetime.now(), spot)   # feeds the local 5-min candles
                updated = True
            except Exception as exc:
                logger.debug(f"[{inst_name}] Spot stream error: {exc}")
        if updated:
            _live_state["timestamp"] = datetime.now().isoformat()
            _write_live_state()


def _cost_for(name: str, lot_size: int) -> float:
    """
    Round-trip cost (₹) for one trade of this instrument. Nifty keeps COST_PER_TRADE (216 =
    ~₹66 fees/taxes + 2 premium pts × lot 65 slippage). Others default to ₹66 fees plus
    3 premium points of slippage × lot size (an assumption — override with <NAME>_COST_PER_TRADE).
    """
    override = os.getenv(f"{name}_COST_PER_TRADE")
    if override:
        return float(override)
    return config.COST_PER_TRADE if name == "NIFTY" else 66.0 + 3.0 * lot_size


# Build one trader per instrument and restore today's state from the database.
_costs: dict[str, float] = {}
for _inst in config.INSTRUMENTS:
    try:
        _cfg = md.get_instrument(_inst)
    except ValueError:
        continue                       # reported by _print_startup_banner()
    _costs[_inst]   = _cost_for(_inst, _cfg.lot_size)
    _traders[_inst] = PaperTrader(instrument=_inst, lot_size=_cfg.lot_size, fo_exchange=_cfg.fo_exchange,
                                  cost_per_trade=_costs[_inst])
    _stores[_inst]  = CandleStore(_inst, minutes=config.CANDLE_MINUTES)
    _risks[_inst]   = RiskEngine(expiry_weekday=_cfg.expiry_weekday)
    _closed_today   = _traders[_inst].todays_closed_trades()
    _risks[_inst].restore_from_trades(_closed_today)
    # Once-a-day strategies must remember they already traded before the restart.
    _used = {t.get("strategy") for t in _closed_today}
    if _traders[_inst].open_trade:
        _used.add(_traders[_inst].open_trade.get("strategy"))
    for _name in _used:
        _hook = getattr(ALL_STRATEGIES.get(_name), "on_entry", None)
        if _hook:
            _hook(_inst)

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


def _sleep(secs: float) -> None:
    """Sleep in short slices so a shutdown signal is honoured within a second."""
    end = time.monotonic() + secs
    while _running and (left := end - time.monotonic()) > 0:
        time.sleep(min(1.0, left))


# ── Candle timing ─────────────────────────────────────────────

def _closed_bar_start(now: datetime | None = None) -> datetime:
    """Start time of the most recent FULLY CLOSED candle (e.g. 12:07 → the 12:00 candle)."""
    now = now or datetime.now()
    current = now.replace(minute=now.minute - now.minute % BAR_MIN, second=0, microsecond=0)
    return current - timedelta(minutes=BAR_MIN)


def _next_sleep() -> float:
    """How long to wait before the next tick."""
    if any(not t.is_flat for t in _traders.values()):
        return config.EXIT_POLL_SECS                       # watching a live position
    if _retry_soon:
        return 15.0                                        # waiting on a candle / data hiccup
    now = datetime.now()
    next_close = _closed_bar_start(now) + timedelta(minutes=2 * BAR_MIN)
    wait = (next_close - now).total_seconds() + config.CANDLE_SETTLE_SECS
    return max(3.0, min(wait, POLL_INTERVAL))


def _maybe_refresh_vix() -> None:
    global _last_vix_t
    if time.monotonic() - _last_vix_t < VIX_REFRESH_SECS:
        return
    _last_vix_t = time.monotonic()
    vix = md.get_india_vix()
    if vix > 0:                       # keep the previous value if the fetch failed
        _live_state["vix"] = round(vix, 2)


# ── Per-tick logic ────────────────────────────────────────────

def _tick() -> float:
    """One iteration of the main loop. Returns seconds to sleep before the next one."""
    global _retry_soon
    _retry_soon = False

    if _is_weekend():
        _live_state["timestamp"] = datetime.now().isoformat()
        _live_state["regime"]    = "WEEKEND"
        _write_live_state()
        return 600

    if not _is_market_open():
        _live_state["timestamp"] = datetime.now().isoformat()
        _live_state["regime"]    = "CLOSED"
        _write_live_state()
        return 30

    sm = SessionManager.get()
    _live_state["timestamp"] = datetime.now().isoformat()

    # Shared blackout check (applies to all instruments)
    blackout, reason = list(_risks.values())[0].is_blackout_day()
    if blackout:
        _live_state["regime"] = f"BLACKOUT: {reason}"
        _write_live_state()
        logger.info(f"🚫 {reason}")
        return 300

    vix = float(_live_state.get("vix") or 0.0)
    for inst_name in config.INSTRUMENTS:
        if inst_name not in _traders:
            continue
        try:
            _tick_instrument(inst_name, vix, sm)
        except Exception as exc:
            logger.error(f"[{inst_name}] Tick error: {exc}", exc_info=True)
            _retry_soon = True

    _maybe_refresh_vix()
    _write_live_state()
    return _next_sleep()


def _close_position(inst_name: str, ltp: float, reason: str) -> None:
    """Exit the open trade and record the result (net of costs) in the risk engine."""
    trader, risk = _traders[inst_name], _risks[inst_name]
    entry = trader.open_trade["entry_price"]
    net = trader.exit(ltp, reason)
    risk.record_trade(ltp - entry, net_inr=net)


def _tick_instrument(inst_name: str, vix: float, sm) -> None:
    """Process one instrument per tick — manage an open position, or look for an entry."""
    global _retry_soon

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
    istate["daily_pnl"]   = round(trader.daily_pnl, 2)
    istate["trade_count"] = trader.trade_count

    # ── Open position: fast path (stop / target / trail on live option price) ──
    if not trader.is_flat:
        t     = trader.open_trade
        token = t.get("symbol_token", "")
        try:
            ltp = md.get_ltp(t["symbol"], token, exchange=inst.fo_exchange)
        except Exception as exc:
            if _is_force_exit_time():
                ltp = t["entry_price"]               # can't price it; close flat rather than hold overnight
            else:
                logger.error(f"[{inst_name}] LTP fetch error: {exc}")
                return

        if _is_force_exit_time():
            logger.warning(f"[{inst_name}] ⏰ EOD force-close")
            _close_position(inst_name, ltp, "EOD_FORCE_EXIT")
            istate["status"] = "EOD"
            return

        trader.update_peak(ltp)
        entry        = t["entry_price"]
        floating_pts = ltp - entry
        istate.update({
            "status":           "IN_TRADE",
            "ltp":              round(ltp, 2),
            "entry_price":      round(entry, 2),
            "floating_pnl":     round(floating_pts, 2),
            "floating_pnl_inr": round(floating_pts * trader.lot_size, 2),
            "symbol":           t["symbol"],
            "option_type":      t["option_type"],
            "strike":           t["strike"],
            "strategy":         t.get("strategy") or istate.get("strategy", "—"),
        })

        should_exit, exit_reason = risk.exit_check(
            entry, ltp, t["peak_price"],
            sl_pts=t.get("sl_pts"), tgt_pts=t.get("tgt_pts"),
            trail_at=t.get("trail_at_pts"), trail_dist=t.get("trail_dist_pts"),
        )
        if should_exit:
            _close_position(inst_name, ltp, exit_reason)
            istate.update({"status": "FLAT", "ltp": 0.0, "floating_pnl": 0.0, "floating_pnl_inr": 0.0,
                           "daily_pnl": round(trader.daily_pnl, 2), "trade_count": trader.trade_count})
        return

    # ── FLAT ──────────────────────────────────────────────────
    istate.update({"status": "FLAT", "ltp": 0.0, "floating_pnl": 0.0, "floating_pnl_inr": 0.0})

    if not _is_entry_window():
        return

    # Daily limits apply to the WHOLE portfolio, not per instrument — otherwise two
    # instruments could each lose the full daily limit.
    total_pnl = sum(t.daily_pnl for t in _traders.values())
    if total_pnl >= config.DAILY_PROFIT_TARGET:
        can_trade, block_reason = False, f"Portfolio daily target ₹{config.DAILY_PROFIT_TARGET:.0f} reached (₹{total_pnl:+.0f})"
    elif total_pnl <= -config.DAILY_MAX_LOSS:
        can_trade, block_reason = False, f"Portfolio daily loss limit ₹{config.DAILY_MAX_LOSS:.0f} hit (₹{total_pnl:+.0f})"
    else:
        can_trade, block_reason = risk.can_trade()
    if not can_trade:
        if istate.get("regime") != f"BLOCKED: {block_reason}":
            logger.info(f"[{inst_name}] 🔒 {block_reason}")
        istate["regime"] = f"BLOCKED: {block_reason}"
        return

    # Decide once per CLOSED candle, shortly after it closes.
    bar = _closed_bar_start()
    if _last_eval_bar.get(inst_name) == bar:
        return
    secs_since_close = (datetime.now() - (bar + timedelta(minutes=BAR_MIN))).total_seconds()
    if secs_since_close < config.CANDLE_SETTLE_SECS:
        _retry_soon = True                 # candle just closed — give the API a moment to publish it
        return

    store = _stores[inst_name]
    try:
        spot = md.get_spot(inst_name)
    except Exception as exc:
        logger.error(f"[{inst_name}] Market data error: {exc}")
        istate["status"] = "DATA_ERR"
        _retry_soon = True
        return
    istate["spot"] = round(spot, 2)
    store.add_tick(datetime.now(), spot)

    # Candles are built locally from price samples. The rate-limited historical API is only
    # asked to fill a gap or to reconcile with official candles — and a failure there never
    # blocks or delays a decision.
    if store.api_due(bar):
        try:
            n = store.merge_api(md.get_index_candles(inst_name, interval=CANDLE_INTERVAL, use_cache=False), bar)
            store.note_api(True)
            logger.info(f"[{inst_name}] 🕯️  official candles merged ({n} bars)")
        except Exception as exc:
            store.note_api(False)
            logger.info(f"[{inst_name}] candle API unavailable ({str(exc)[:70]}) — using local candles")

    df = store.closed(bar)
    if df.empty or df["datetime"].iloc[-1] < bar:
        _candle_waits[inst_name] = _candle_waits.get(inst_name, 0) + 1
        if _candle_waits[inst_name] <= MAX_CANDLE_WAITS:
            logger.debug(f"[{inst_name}] Waiting for the {bar:%H:%M} candle to be published")
            _retry_soon = True
            return
        logger.warning(f"[{inst_name}] {bar:%H:%M} candle still missing — deciding on the latest available")
    _candle_waits[inst_name] = 0
    _last_eval_bar[inst_name] = bar
    if len(df) < 30:
        return

    # ── Option chain (optional; disabled when the SDK lacks getOptionGreeks) ──
    option_chain = None
    try:
        option_chain = fetch_option_chain(sm.smart(), spot, md.get_expiry_string(inst_name))
        if option_chain:
            istate["pcr"]      = round(option_chain.get("pcr", 0.0), 2)
            istate["max_pain"] = int(option_chain.get("max_pain", 0))
    except Exception as exc:
        logger.debug(f"[{inst_name}] Option chain unavailable: {exc}")

    # ── Indicator panel (last COMPLETED candle — the one the strategies see) ──
    try:
        from strategy import compute_supertrend, compute_ema, compute_rsi, compute_vwap
        _row = compute_vwap(compute_rsi(compute_ema(compute_supertrend(df)))).iloc[-1]
        istate["supertrend"] = _row["supertrend_dir"] if _row["supertrend_dir"] in ("UP", "DOWN") else "—"
        istate["ema_cross"]  = "BULL" if _row["ema_fast"] > _row["ema_slow"] else "BEAR"
        istate["rsi"]        = round(float(_row["rsi"]), 1)
        istate["vwap_pos"]   = "ABOVE" if _row["close"] > _row["vwap"] else "BELOW"
    except Exception as exc:
        logger.warning(f"[{inst_name}] Indicator panel error: {exc}")

    signal, regime = select_strategy(
        df_5min=df, df_1min=df,
        vix=vix, option_chain=option_chain, spot=spot,
        instrument=inst_name, lot_size=trader.lot_size, cost=trader.cost_per_trade,
    )

    # Always write regime + signal to live state (even if no trade)
    _live_state["regime"] = regime or "—"
    istate["regime"] = regime or "—"
    if signal:
        istate["strategy"]      = signal.strategy or "—"
        istate["confidence"]    = signal.confidence
        istate["signal_reason"] = (signal.reason or "")[:80]

    if signal is None or signal.signal.value != "BUY" or signal.option_type is None:
        logger.info(
            f"[{inst_name}] {bar:%H:%M} candle closed {df['close'].iloc[-1]:.1f} → no entry | "
            f"regime={regime} ST={istate['supertrend']} EMA={istate['ema_cross']} "
            f"RSI={istate['rsi']} VWAP={istate['vwap_pos']} | enabled={','.join(sorted(ENABLED_STRATEGIES))}"
        )
        return

    if signal.confidence < MIN_CONFIDENCE:
        logger.debug(f"[{inst_name}] Confidence {signal.confidence} < {MIN_CONFIDENCE}")
        return

    # Resolve symbol, token, LTP
    try:
        strike     = md.get_atm_strike(spot, strike_gap=inst.strike_gap)
        opt_type   = signal.option_type.value
        # Exact symbol + token from the scrip master (handles NFO/BFO symbol styles and needs no
        # rate-limited searchScrip call); falls back to building the symbol and searching for it.
        contract = md.find_contract(inst_name, strike, opt_type)
        if contract:
            symbol, token = contract
        else:
            logger.warning(f"[{inst_name}] {strike}{opt_type} not in scrip-master table — using searchScrip")
            symbol = md.build_option_symbol(inst_name, md.get_expiry_string(inst_name), strike, opt_type)
            token  = md.get_symbol_token(symbol, exchange=inst.fo_exchange)
        ltp        = md.get_ltp(symbol, token, exchange=inst.fo_exchange)
    except Exception as exc:
        logger.error(f"[{inst_name}] Symbol resolution error: {exc}")
        return

    # Liquidity check
    liquid, liq_reason = md.check_liquidity(symbol, token, exchange=inst.fo_exchange)
    if not liquid:
        logger.warning(f"[{inst_name}] 🚫 {liq_reason}")
        return

    # Exit rules travel with the trade. A strategy that gives neither a target nor a
    # trail gets the global target.
    sl_pts  = signal.suggested_sl_pts if signal.suggested_sl_pts is not None else config.STOP_LOSS_POINTS
    tgt_pts = signal.suggested_tgt_pts
    if tgt_pts is None and signal.trail_at_pts is None:
        tgt_pts = config.PROFIT_TARGET_POINTS

    trader.enter(
        symbol=symbol, strike=strike, option_type=opt_type, ltp=ltp,
        symbol_token=token, confidence=signal.confidence,
        signal_detail=f"{inst_name}|{signal.strategy}|{signal.reason[:60]}",
        sl_pts=sl_pts, tgt_pts=tgt_pts,
        trail_at_pts=signal.trail_at_pts, trail_dist_pts=signal.trail_dist_pts,
        strategy=signal.strategy,
    )
    if not trader.is_flat:
        on_entry = getattr(ALL_STRATEGIES.get(signal.strategy), "on_entry", None)
        if on_entry:
            on_entry(inst_name)
        logger.info(
            f"[{inst_name}] exits → stop -{sl_pts} pts"
            + (f", target +{tgt_pts} pts" if tgt_pts else ", no fixed target")
            + (f", trail after +{signal.trail_at_pts} by {signal.trail_dist_pts} pts"
               if signal.trail_at_pts else "")
        )


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
            f"  → expiry={expiry_str}  lot={lot_src}  cost=₹{_costs.get(inst_name, 0):.0f}/trade"
        )

    logger.info("=" * 72)
    logger.info(f"  Strategies   : {', '.join(sorted(ENABLED_STRATEGIES)) or 'NONE'}")
    logger.info(f"  Decisions    : once per closed {BAR_MIN}-min candle (+{config.CANDLE_SETTLE_SECS}s)")
    logger.info(f"  Entry window : {config.ENTRY_TIME_START} – {config.ENTRY_TIME_END} IST")
    logger.info(f"  Force exit   : {config.EXIT_ALL_TIME} IST   | exit checks every {config.EXIT_POLL_SECS}s in a trade")
    logger.info(f"  Daily target : ₹{config.DAILY_PROFIT_TARGET} / Max loss: ₹{config.DAILY_MAX_LOSS} (whole portfolio, all instruments)"
                f"  | max loss per trade ₹{config.MAX_LOSS_PER_TRADE} (incl. costs)")
    logger.info("=" * 72)
    if config.TRADING_MODE == "live":
        logger.warning("⚠️  LIVE MODE — REAL ORDERS WILL BE PLACED.  Ctrl+C to abort now.")
        time.sleep(5)   # 5-second window to abort before first tick


# ── Main entry ────────────────────────────────────────────────

def run():
    md.refresh_tables()          # starts the scrip-master download in the background if the saved table is old
    _print_startup_banner()
    export_history()      # CSV + snapshot of everything saved so far

    # ── Step 1: Login only (no heartbeat yet) ─────────────────────────
    try:
        SessionManager.get().connect()
    except Exception as exc:
        logger.critical(f"❌ Login failed: {exc}")
        sys.exit(1)

    # ── Step 2: Start heartbeat (it sleeps 60s before first ping) ─────
    SessionManager.get().start_heartbeat()

    # ── Step 3: Warmup ────────────────────────────────────────────────
    # generateSession counts against Angel One's historical-data quota, so a normal
    # start waits 75s before the first candle request. If a position was restored
    # after a restart, we need to watch it (live-price calls only), so wait just 10s.
    holding   = any(not t.is_flat for t in _traders.values())
    bar       = _closed_bar_start()
    need_api  = any(s.has_gap(bar) for s in _stores.values())   # saved candles incomplete → API call coming
    warmup    = 10 if (holding or not need_api) else 75   # position monitoring needs no candle API
    md._last_api_call_t = time.monotonic()   # throttle starts counting from now
    if holding:
        why = " (open position restored — monitoring it first)"
    elif not need_api:
        why = " (saved candles are complete — no candle API call needed)"
    else:
        why = ""
    logger.info(f"⏳ Waiting {warmup}s for Angel One rate-limit window to clear..." + why)
    _sleep(warmup)
    logger.info("✅ Session ready — starting market loop")

    if LIVE_SPOT_INTERVAL > 0:
        threading.Thread(target=_spot_stream_loop, name="spot-stream", daemon=True).start()

    while _running:
        try:
            wait = _tick()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(f"Tick error: {exc}", exc_info=True)
            wait = 15
        _sleep(wait)

    # ── Cleanup ───────────────────────────────────────────────
    # Open positions are deliberately NOT closed here: they stay in the database and
    # are restored on the next start (a restart must not invent an exit price or P&L).
    for inst_name, trader in _traders.items():
        if not trader.is_flat:
            logger.warning(f"[{inst_name}] Position left OPEN for restart: "
                           f"{trader.open_trade['symbol']} @ ₹{trader.open_trade['entry_price']:.2f}")
    export_history()
    SessionManager.get().disconnect()
    logger.info("👋 NiftyBot stopped.")


if __name__ == "__main__":
    run()
