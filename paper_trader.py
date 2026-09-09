"""
paper_trader.py — Simulated order execution engine.

Records all trades to SQLite (data/trades.db).
No real orders are ever placed in paper mode.
In live mode, routes to Angel One SmartAPI placeOrder.
"""

import sqlite3
from datetime import datetime, date
from typing import Optional
from loguru import logger

import config
import auth


# ── Database setup ────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT    NOT NULL,
            entry_time   TEXT    NOT NULL,
            exit_time    TEXT,
            trading_mode TEXT    NOT NULL,
            symbol       TEXT    NOT NULL,
            strike       INTEGER NOT NULL,
            option_type  TEXT    NOT NULL,
            lot_size     INTEGER NOT NULL,
            entry_price  REAL    NOT NULL,
            exit_price   REAL,
            pnl_points   REAL,
            pnl_inr      REAL,
            exit_reason  TEXT,
            confidence   INTEGER,
            signal_detail TEXT
        )
    """)
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
        symbol:      str,
        strike:      int,
        option_type: str,
        ltp:         float,
        confidence:  int = 0,
        signal_detail: str = "",
    ) -> None:
        """
        Open a new trade position.
        In paper mode: logs to DB only.
        In live mode:  places real order via SmartAPI.
        """
        if not self.is_flat:
            logger.warning("⚠️  Already in a trade — skipping entry.")
            return
        if not self.can_trade:
            return

        now = datetime.now()
        self.open_trade = {
            "symbol":       symbol,
            "strike":       strike,
            "option_type":  option_type,
            "entry_price":  ltp,
            "entry_time":   now,
        }

        db = get_db()
        cur = db.execute("""
            INSERT INTO trades
              (date, entry_time, trading_mode, symbol, strike, option_type,
               lot_size, entry_price, confidence, signal_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            now.date().isoformat(),
            now.isoformat(),
            config.TRADING_MODE,
            symbol, strike, option_type,
            config.LOT_SIZE, ltp,
            confidence, signal_detail,
        ))
        db.commit()
        self.open_trade_db_id = cur.lastrowid
        self._trade_count += 1

        mode_tag = "📝 PAPER" if config.TRADING_MODE == "paper" else "🔴 LIVE"
        logger.info(
            f"{mode_tag} ENTRY | {symbol} {strike}{option_type} "
            f"@ ₹{ltp:.2f} | Confidence: {confidence}/4"
        )

        if config.TRADING_MODE == "live":
            self._place_live_order(symbol, option_type, "BUY", ltp)

    # ── Exit ──────────────────────────────────────────────────

    def exit(self, ltp: float, reason: str) -> float:
        """
        Close the open trade position.
        Returns P&L in INR.
        """
        if self.is_flat:
            logger.warning("⚠️  No open trade to exit.")
            return 0.0

        t          = self.open_trade
        entry      = t["entry_price"]
        pnl_points = ltp - entry
        pnl_inr    = pnl_points * config.LOT_SIZE
        now        = datetime.now()
        icon       = "✅" if pnl_inr >= 0 else "❌"

        # Update DB record
        get_db().execute("""
            UPDATE trades
            SET exit_time=?, exit_price=?, pnl_points=?, pnl_inr=?, exit_reason=?
            WHERE id=?
        """, (
            now.isoformat(), ltp, pnl_points, pnl_inr, reason,
            self.open_trade_db_id,
        ))
        get_db().commit()

        self._daily_pnl += pnl_inr
        self.open_trade       = None
        self.open_trade_db_id = None

        logger.info(
            f"{icon} EXIT | {t['symbol']} {t['strike']}{t['option_type']} "
            f"@ ₹{ltp:.2f} | P&L: {pnl_points:+.1f} pts (₹{pnl_inr:+.0f}) "
            f"| Reason: {reason}"
        )
        logger.info(f"💰 Daily P&L: ₹{self._daily_pnl:+.0f}")

        if config.TRADING_MODE == "live":
            self._place_live_order(
                t["symbol"], t["option_type"], "SELL", ltp
            )

        return pnl_inr

    # ── Live order placement ──────────────────────────────────

    def _place_live_order(
        self,
        symbol:      str,
        option_type: str,
        action:      str,   # "BUY" | "SELL"
        ltp:         float,
    ) -> None:
        """
        Place a real order via Angel One SmartAPI.
        Only called when TRADING_MODE=live.
        """
        smart = auth.get_session()
        order_params = {
            "variety":          "NORMAL",
            "tradingsymbol":    symbol,
            "symboltoken":      "",          # populated by caller via market_data
            "transactiontype":  action,
            "exchange":         "NFO",
            "ordertype":        "MARKET",
            "producttype":      "INTRADAY",
            "duration":         "DAY",
            "quantity":         str(config.LOT_SIZE),
            "price":            "0",
            "squareoff":        "0",
            "stoploss":         "0",
        }
        try:
            resp = smart.placeOrder(order_params)
            if resp.get("status"):
                logger.success(f"✅ LIVE ORDER PLACED | {action} {symbol} | Order ID: {resp['data']['orderid']}")
            else:
                logger.error(f"❌ LIVE ORDER FAILED | {resp.get('message')}")
        except Exception as exc:
            logger.error(f"❌ Live order exception: {exc}")


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
    total_pnl = sum(t.get("pnl_inr") or 0 for t in trades)
    wins      = sum(1 for t in trades if (t.get("pnl_inr") or 0) > 0)
    losses    = sum(1 for t in trades if (t.get("pnl_inr") or 0) < 0)
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
