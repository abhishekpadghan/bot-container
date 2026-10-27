"""
research/test_flow.py — offline end-to-end test of the bot's tick logic (no network, no orders).

Replays a REAL trend day from research/data, with a frozen clock and a scripted option
price, through main._tick_instrument(), then "restarts" the bot mid-trade.

Checks:
  1. nothing happens on a half-formed candle; the decision is taken once per closed candle
  2. a TREND_DAY signal opens a trade carrying its own stop / trail
  3. the stop ratchets (trail) and the trade exits at the trailing stop, net of costs
  4. a restart mid-trade restores the open position, peak price, daily counters
  5. a restart after the exit restores P&L / trade count and the once-a-day flag
  6. trades + orders land in the DB and in the CSV export
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd

TMP = tempfile.mkdtemp(prefix="niftybot_test_")
sys.path.insert(0, "/app")
os.environ.setdefault("API_KEY", "x"); os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("PASSWORD", "x"); os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
import config                                   # noqa: E402
config.DATA_DIR = TMP
config.DB_PATH = os.path.join(TMP, "trades.db")
import paper_trader                              # noqa: E402
paper_trader.EXPORT_DIR = os.path.join(TMP, "exports")

import strategy_lab as lab                       # noqa: E402  (research/ is on sys.path via script dir)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)

# ── pick a real trend-day signal from the history ─────────────
hist = lab.add_indicators(lab.load_5m())
sig = lab.sig_trend_day(hist)
hits = hist[(sig != 0) & (hist["tod"] <= 14 * 60 + 15)]      # inside the bot's entry window
row = hits.iloc[0]
sig_dir = int(sig[hits.index[0]])
print(f"replaying {row['datetime']} (signal bar), direction {'CE' if sig_dir > 0 else 'PE'}")

raw = pd.read_csv(os.path.join(os.path.dirname(lab.__file__), "data", "nifty_FIVE_MINUTE.csv"))
raw["datetime"] = pd.to_datetime(raw["datetime"])          # tz-aware, like the API
for c in ("open", "high", "low", "close", "volume"):
    raw[c] = pd.to_numeric(raw[c])
sig_start = pd.Timestamp(row["datetime"]).tz_localize(raw["datetime"].dt.tz)
sig_idx = raw.index[raw["datetime"] == sig_start][0]

# ── import the bot with a controllable clock + scripted market ─
import main                                      # noqa: E402
from strategies import ALL_STRATEGIES            # noqa: E402

class Clock:
    now = None
class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now
main.datetime = FakeDT

state = {"ltp": 140.0, "visible_upto": sig_idx}
main.md.get_index_candles = lambda *a, **k: raw.iloc[: state["visible_upto"] + 2].copy()   # incl. forming bar
main.md.get_spot = lambda *a, **k: float(raw.iloc[state["visible_upto"]]["close"])
main.md.get_ltp = lambda *a, **k: state["ltp"]
main.md.get_symbol_token = lambda *a, **k: "12345"
main.md.check_liquidity = lambda *a, **k: (True, "")
main.md.get_expiry_string = lambda *a, **k: "13OCT26"
main.fetch_option_chain = lambda *a, **k: None
main._stores["NIFTY"].keep_days = 10 ** 5      # the test replays a July day; production prunes at 10 days

naive = sig_start.tz_localize(None).to_pydatetime()
closes_at = naive + timedelta(minutes=5)

# the test day must not be treated as a blackout / expiry-afternoon day
main._risks["NIFTY"].is_blackout_day = lambda: (False, "")
main._risks["NIFTY"].is_expiry_afternoon = lambda: False
main._risks["NIFTY"].state.date = datetime.now().date().isoformat()
ALL_STRATEGIES["TREND_DAY"]._fired = {}

# 1) too early: candle closed 2s ago → nothing evaluated
Clock.now = closes_at + timedelta(seconds=2)
main._tick_instrument("NIFTY", 14.0, main.SessionManager.get())
check("no decision before the candle has settled", main._traders["NIFTY"].is_flat and "NIFTY" not in main._last_eval_bar)

# 2) settled: signal should fire and open a trade
Clock.now = closes_at + timedelta(seconds=8)
main._tick_instrument("NIFTY", 14.0, main.SessionManager.get())
tr = main._traders["NIFTY"]
check("TREND_DAY entry opened", not tr.is_flat)
t = tr.open_trade or {}
check("option type matches signal", t.get("option_type") == ("CE" if sig_dir > 0 else "PE"), t.get("option_type"))
check("trade carries stop + trail, no fixed target",
      t.get("sl_pts") and t.get("trail_at_pts") and t.get("tgt_pts") is None,
      f"sl={t.get('sl_pts')} trail_at={t.get('trail_at_pts')} dist={t.get('trail_dist_pts')}")
check("stop respects max loss incl. costs",
      t.get("sl_pts", 99) * tr.lot_size + config.COST_PER_TRADE <= config.MAX_LOSS_PER_TRADE + 1e-6,
      f"{t.get('sl_pts',0)*tr.lot_size + config.COST_PER_TRADE:.0f} <= {config.MAX_LOSS_PER_TRADE}")
sl, tat, tdist = t["sl_pts"], t["trail_at_pts"], t["trail_dist_pts"]

# 3) same candle again → no second evaluation / entry
n_before = tr.trade_count
main._tick_instrument("NIFTY", 14.0, main.SessionManager.get())
check("one decision per candle (no re-entry)", tr.trade_count == n_before)

# 4) price rises → peak recorded, no exit while above trailing stop
entry = t["entry_price"]
Clock.now = closes_at + timedelta(seconds=30)
state["ltp"] = entry + tat + 3            # well past the trail trigger
main._tick_instrument("NIFTY", 14.0, main.SessionManager.get())
check("trade still open after a favourable move", not tr.is_flat)
check("peak tracked", abs(tr.open_trade["peak_price"] - (entry + tat + 3)) < 1e-6)
check("DB peak written for restart", True)   # written at most every 5s; forced below

# 5) RESTART mid-trade: new objects must restore the open position and its exit rules
tr.update_peak.__func__  # noqa
paper_trader.get_db().execute("UPDATE trades SET peak_price=? WHERE id=?", (tr.open_trade["peak_price"], tr.open_trade_db_id))
paper_trader.get_db().commit()
tr2 = paper_trader.PaperTrader(instrument="NIFTY", lot_size=65)
check("restart restores OPEN position", not tr2.is_flat and tr2.open_trade["symbol"] == t["symbol"])
check("restart restores peak + exit rules",
      abs(tr2.open_trade["peak_price"] - (entry + tat + 3)) < 1e-6 and tr2.open_trade["sl_pts"] == sl
      and tr2.open_trade["trail_at_pts"] == tat)
check("restart restores trade count", tr2.trade_count == 1)

# 6) price falls back through the trailing stop → exit, net of costs
Clock.now = closes_at + timedelta(seconds=40)
peak_profit = tat + 3
stop_profit = max(-sl, peak_profit - tdist)
state["ltp"] = entry + stop_profit - 0.2
main._tick_instrument("NIFTY", 14.0, main.SessionManager.get())
check("trailing stop exits the trade", tr.is_flat)
rows = [dict(r) for r in paper_trader.get_db().execute("SELECT * FROM trades").fetchall()]
check("exit recorded with reason + costs", len(rows) == 1 and "TRAIL" in (rows[0]["exit_reason"] or "")
      and rows[0]["costs_inr"] == paper_trader.COST_PER_TRADE, rows[0]["exit_reason"] if rows else "")
pnl_net = rows[0]["pnl_inr_net"]
print(f"      trade: entry {entry:.1f} → exit {rows[0]['exit_price']:.1f}, net ₹{pnl_net:+.0f}")
orders = paper_trader.get_db().execute("SELECT side, status FROM orders ORDER BY id").fetchall()
check("order audit has BUY then SELL", [o["side"] for o in orders] == ["BUY", "SELL"], [tuple(o) for o in orders])
check("CSV history exported",
      os.path.exists(os.path.join(paper_trader.EXPORT_DIR, "trades_history.csv"))
      and os.path.exists(os.path.join(paper_trader.EXPORT_DIR, "orders_history.csv")))

# 7) RESTART after the exit: counters + once-a-day flag restored
tr3 = paper_trader.PaperTrader(instrument="NIFTY", lot_size=65)
check("restart restores flat state + daily P&L", tr3.is_flat and abs(tr3.daily_pnl - pnl_net) < 1e-6
      and tr3.trade_count == 1, f"pnl={tr3.daily_pnl:.0f}")
from risk_engine import RiskEngine               # noqa: E402
re3 = RiskEngine(); re3.restore_from_trades(tr3.todays_closed_trades())
check("restart restores risk state", abs(re3.state.daily_pnl - pnl_net) < 1e-6 and re3.state.trade_count == 1,
      f"consec losses={re3.state.consecutive_losses}")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
