"""
paper_trader.py — Simulated order execution engine.

Records all trades to SQLite (data/trades.db).
No real orders are ever placed in paper mode.
In live mode, routes to Angel One SmartAPI placeOrder.

Blocker fixes:
  - symboltoken stored on open_trade and passed to live orders (was empty "")
  - Slippage + brokerage deducted from every P&L (COST_PER_TRADE)
  - Order fill status verified after placeOrder (rejected orders abort trade)
"""

import os
import time
import sqlite3
from datetime import datetime, date
from typing import Optional
from loguru import logger

import config

# ── Transaction cost constants ────────────────────────────────
# Deducted from every trade P&L to reflect real-world costs.
# Per round-trip (buy + sell) for 1 lot Nifty options:
#   Brokerage Angel One flat  : ₹20 × 2           = ₹40
#   STT (sell side 0.05%)     : 0.05% × premium   ≈ ₹10
#   Exchange + SEBI charge    : 0.05% of turnover ≈ ₹8
#   GST on brokerage (18%)    : ₹40 × 0.18        = ₹7
#   Stamp duty (0.003% buy)   : ~₹1
#   Slippage (2 pts × ₹75)    : ₹150
#   ────────────────────────────────────────────────────────────
#   TOTAL ESTIMATED COST/TRADE: ₹216 (override via COST_PER_TRADE in .env)
COST_PER_TRADE = float(os.getenv("COST_PER_TRADE", "216"))


# ── Database setup ────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,
            entry_time    TEXT    NOT NULL,
            exit_time     TEXT,
            trading_mode  TEXT    NOT NULL,
            symbol        TEXT    NOT NULL,
            symbol_token  TEXT,
            strike        INTEGER NOT NULL,
            option_type   TEXT    NOT NULL,
            lot_size      INTEGER NOT NULL,
            entry_price   REAL    NOT NULL,
            exit_price    REAL,
            pnl_points    REAL,
            pnl_inr_gross REAL,
            costs_inr     REAL    DEFAULT 0,
            pnl_inr_net   REAL,
            exit_reason   TEXT,
            order_id      TEXT,
            confidence    INTEGER,
            signal_detail TEXT
        )
    """)
    # Add new columns to existing DB if upgrading from older schema
    for col, typedef in [
        ("symbol_token",  "TEXT"),
        ("pnl_inr_gross", "REAL"),
        ("costs_inr",     "REAL DEFAULT 0"),
        ("pnl_inr_net",   "REAL"),
        ("order_id",      "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # column already exists
    conn.commit()
    return conn


_conn: Optional[sqlite3.Connection] = None

def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


# ── PaperTrader ───────────────────────────────────────────────

class PaperTrader:
    """
    Manages the lifecycle of a single trade position.

    State machine:
        FLAT → (entry signal) → IN_TRADE → (exit signal) → FLAT

    Tracks:
        - Open trade details
        - Daily P&L (resets on new trading day)
        - Trade count for the day
    """

    def __init__(self) -> None:
        self.open_trade: Optional[dict]  = None
        self.open_trade_db_id: Optional[int] = None
        self._daily_pnl:    float = 0.0
        self._trade_count:  int   = 0
        self._last_date:    date  = date.today()
        self._reset_if_new_day()

    # ── Daily reset ───────────────────────────────────────────

    def _reset_if_new_day(self) -> None:
        """Reset daily counters at the start of a new trading day."""
        today = date.today()
        if today != self._last_date:
            logger.info(
                f"📅 New day {today} — resetting daily P&L "
                f"(yesterday: ₹{self._daily_pnl:+.0f})"
            )
            self._daily_pnl   = 0.0
            self._trade_count = 0
            self._last_date   = today

    # ── Properties ────────────────────────────────────────────

    @property
    def daily_pnl(self) -> float:
        self._reset_if_new_day()
        return self._daily_pnl

    @property
    def trade_count(self) -> int:
        self._reset_if_new_day()
        return self._trade_count

    @property
    def is_flat(self) -> bool:
        return self.open_trade is None

    @property
    def can_trade(self) -> bool:
        """Returns False if daily limits are already hit."""
        self._reset_if_new_day()
        if self._daily_pnl >= config.DAILY_PROFIT_TARGET:
            logger.info(f"🎯 Daily target ₹{config.DAILY_PROFIT_TARGET} reached — no more trades today")
            return False
        if self._daily_pnl <= -config.DAILY_MAX_LOSS:
            logger.warning(f"🛑 Daily loss limit ₹{config.DAILY_MAX_LOSS} hit — stopping for today")
            return False
        if self._trade_count >= config.MAX_TRADES_PER_DAY:
            logger.info(f"🔢 Max {config.MAX_TRADES_PER_DAY} trades/day reached — done for today")
            return False
        return True

    # ── Entry ─────────────────────────────────────────────────

    def enter(
        self,
        symbol:        str,
        strike:        int,
        option_type:   str,
        ltp:           float,
        symbol_token:  str = "",   # BLOCKER 1 FIX: token passed in and stored
        confidence:    int = 0,
        signal_detail: str = "",
    ) -> None:
        """
        Open a new trade position.
        In paper mode: logs to DB only.
        In live mode:  places real order via SmartAPI with correct symboltoken.
        """
        if not self.is_flat:
            logger.warning("⚠️  Already in a trade — skipping entry.")
            return
        if not self.can_trade:
            return

        now = datetime.now()
        order_id = None

        # ── Live mode: place real BUY order first ─────────────
        if config.TRADING_MODE == "live":
            order_id = self._place_live_order(
                symbol, symbol_token, option_type, "BUY", ltp
            )
            if order_id is None:
                logger.error("❌ BUY order rejected — aborting entry")
                return   # abort: don't track a trade that wasn't placed

        self.open_trade = {
            "symbol":       symbol,
            "symbol_token": symbol_token,
            "strike":       strike,
            "option_type":  option_type,
            "entry_price":  ltp,
            "entry_time":   now,
            "order_id":     order_id,
        }

        db = get_db()
        cur = db.execute("""
            INSERT INTO trades
              (date, entry_time, trading_mode, symbol, symbol_token,
               strike, option_type, lot_size, entry_price,
               order_id, confidence, signal_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now.date().isoformat(), now.isoformat(), config.TRADING_MODE,
            symbol, symbol_token, strike, option_type,
            config.LOT_SIZE, ltp, order_id, confidence, signal_detail,
        ))
        db.commit()
        self.open_trade_db_id = cur.lastrowid
        self._trade_count += 1

        mode_tag = "📝 PAPER" if config.TRADING_MODE == "paper" else "🔴 LIVE"
        logger.info(
            f"{mode_tag} ENTRY | {symbol} {strike}{option_type} "
            f"@ ₹{ltp:.2f} | Confidence: {confidence}/4"
            + (f" | OrderID: {order_id}" if order_id else "")
        )

    # ── Exit ──────────────────────────────────────────────────

    def exit(self, ltp: float, reason: str) -> float:
        """
        Close the open trade position.
        Returns P&L in INR.
        """
        if self.is_flat:
            logger.warning("⚠️  No open trade to exit.")
            return 0.0

        t            = self.open_trade
        entry        = t["entry_price"]
        pnl_points   = ltp - entry
        pnl_inr_gross = pnl_points * config.LOT_SIZE

        # BLOCKER 2 FIX: deduct realistic transaction costs from every trade
        costs        = COST_PER_TRADE
        pnl_inr_net  = pnl_inr_gross - costs
        now          = datetime.now()
        icon         = "✅" if pnl_inr_net >= 0 else "❌"

        sell_order_id = None

        # ── Live mode: place real SELL order ──────────────────
        if config.TRADING_MODE == "live":
            sell_order_id = self._place_live_order(
                t["symbol"], t.get("symbol_token", ""),
                t["option_type"], "SELL", ltp
            )
            if sell_order_id is None:
                logger.error("❌ SELL order rejected — position may still be open!")
                # Still record the attempted exit but flag it
                reason = f"SELL_FAILED|{reason}"

        # Update DB record with full cost breakdown
        get_db().execute("""
            UPDATE trades
            SET exit_time=?, exit_price=?, pnl_points=?,
                pnl_inr_gross=?, costs_inr=?, pnl_inr_net=?,
                exit_reason=?
            WHERE id=?
        """, (
            now.isoformat(), ltp, pnl_points,
            pnl_inr_gross, costs, pnl_inr_net,
            reason, self.open_trade_db_id,
        ))
        get_db().commit()

        # Track net P&L (after costs) for daily limit checks
        self._daily_pnl += pnl_inr_net
        self.open_trade       = None
        self.open_trade_db_id = None

        logger.info(
            f"{icon} EXIT | {t['symbol']} {t['strike']}{t['option_type']} "
            f"@ ₹{ltp:.2f} | Gross: {pnl_points:+.1f}pts (₹{pnl_inr_gross:+.0f}) "
            f"| Costs: ₹{costs:.0f} | Net: ₹{pnl_inr_net:+.0f} | {reason}"
        )
        logger.info(f"💰 Daily Net P&L: ₹{self._daily_pnl:+.0f}")
        return pnl_inr_net

    # ── Live order placement ──────────────────────────────────

    def _place_live_order(
        self,
        symbol:       str,
        symbol_token: str,    # BLOCKER 1 FIX: was always "" before
        option_type:  str,
        action:       str,    # "BUY" | "SELL"
        ltp:          float,
    ) -> Optional[str]:
        """
        Place a real order via Angel One SmartAPI.
        Only called when TRADING_MODE=live.

        Returns:
            order_id (str)  on success
            None            on failure — caller must abort the trade

        BLOCKER 4 FIX: verifies order is filled before returning.
        """
        from session_manager import get_smart
        smart = get_smart()

        if not symbol_token:
            logger.error(f"❌ symboltoken is empty for {symbol} — cannot place order")
            return None

        order_params = {
            "variety":         "NORMAL",
            "tradingsymbol":   symbol,
            "symboltoken":     symbol_token,   # ← was "" before
            "transactiontype": action,
            "exchange":        "NFO",
            "ordertype":       "MARKET",
            "producttype":     "INTRADAY",
            "duration":        "DAY",
            "quantity":        str(config.LOT_SIZE),
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
        }

        try:
            resp = smart.placeOrder(order_params)
        except Exception as exc:
            logger.error(f"❌ placeOrder exception: {exc}")
            return None

        if not resp.get("status"):
            logger.error(
                f"❌ LIVE ORDER REJECTED | {action} {symbol} | "
                f"Reason: {resp.get('message', 'unknown')}"
            )
            return None

        order_id = resp["data"]["orderid"]
        logger.info(f"📤 Order placed | {action} {symbol} | OrderID: {order_id}")

        # BLOCKER 4 FIX: Poll order book to confirm fill (max 10s)
        filled_price = self._wait_for_fill(smart, order_id, timeout_secs=10)
        if filled_price is None:
            logger.error(
                f"❌ Order {order_id} not filled within 10s — "
                f"check Angel One app immediately!"
            )
            return None

        logger.success(
            f"✅ LIVE ORDER FILLED | {action} {symbol} "
            f"@ ₹{filled_price:.2f} | OrderID: {order_id}"
        )
        return order_id

    def _wait_for_fill(
        self,
        smart,
        order_id: str,
        timeout_secs: int = 10,
    ) -> Optional[float]:
        """
        Poll the order book until the order is complete or timeout.

        Returns:
            filled average price (float) if filled
            None if rejected, cancelled, or timed out
        """
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            try:
                book = smart.orderBook()
                if not book.get("status") or not book.get("data"):
                    time.sleep(1)
                    continue
                for order in book["data"]:
                    if str(order.get("orderid")) == str(order_id):
                        status = order.get("orderstatus", "").upper()
                        if status == "COMPLETE":
                            return float(order.get("averageprice", 0) or 0)
                        if status in ("REJECTED", "CANCELLED"):
                            logger.error(
                                f"❌ Order {order_id} {status}: "
                                f"{order.get('text', '')}"
                            )
                            return None
                        # Still OPEN or TRIGGER_PENDING — wait
            except Exception as exc:
                logger.warning(f"Order book poll error: {exc}")
            time.sleep(1)

        logger.warning(f"⏰ Order {order_id} fill timeout after {timeout_secs}s")
        return None


# ── Daily Summary ─────────────────────────────────────────────

def get_daily_summary(trade_date: Optional[str] = None) -> dict:
    """
    Return a summary dict for a given date (default: today).
    Used by report.py and dashboard.py.
    """
    if trade_date is None:
        trade_date = date.today().isoformat()

    db = get_db()
    rows = db.execute("""
        SELECT * FROM trades WHERE date=? ORDER BY entry_time
    """, (trade_date,)).fetchall()

    trades    = [dict(r) for r in rows]
    # Support both old schema (pnl_inr) and new schema (pnl_inr_net)
    def _net(t):
        return t.get("pnl_inr_net") or t.get("pnl_inr") or 0
    total_pnl = sum(_net(t) for t in trades)
    wins      = sum(1 for t in trades if _net(t) > 0)
    losses    = sum(1 for t in trades if _net(t) < 0 and t.get("exit_time"))
    open_pos  = sum(1 for t in trades if t.get("exit_time") is None)

    return {
        "date":       trade_date,
        "trades":     trades,
        "total_pnl":  total_pnl,
        "wins":       wins,
        "losses":     losses,
        "open":       open_pos,
        "count":      len(trades),
        "target_hit": total_pnl >= config.DAILY_PROFIT_TARGET,
        "limit_hit":  total_pnl <= -config.DAILY_MAX_LOSS,
    }
