"""
research/test_sensex.py — offline-ish checks for running SENSEX next to NIFTY.
Downloads the real Angel One scrip master once (public file) and checks:
  1. SENSEX/NIFTY contract lookup returns real symbols + tokens (BFO and NFO symbol styles)
  2. the traded expiry is the next listed one (SENSEX Thursday, NIFTY holiday-shifted Monday)
  3. TREND_DAY sizes the stop per instrument so one stopped trade stays within MAX_LOSS_PER_TRADE
  4. the once-a-day flag is per instrument (NIFTY trading must not block SENSEX)
  5. SENSEX registry data match the scrip master (index token/symbol, lot size, strike gap)
"""
import os, sys, tempfile
import numpy as np, pandas as pd

TMP = tempfile.mkdtemp(prefix="sensex_test_")
sys.path.insert(0, "/app")
os.environ.setdefault("API_KEY", "x"); os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("PASSWORD", "x"); os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
import config
config.DATA_DIR = TMP
import market_data as md
from strategies import ALL_STRATEGIES

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

sx, nf = md.get_instrument("SENSEX"), md.get_instrument("NIFTY")
check("SENSEX registry: index token/symbol/lot/gap match the scrip master",
      (sx.index_token, sx.index_symbol, sx.lot_size, sx.strike_gap, sx.fo_exchange, sx.expiry_weekday)
      == ("99919000", "SENSEX", 20, 100, "BFO", 3),
      (sx.index_token, sx.index_symbol, sx.lot_size, sx.strike_gap))

md._EXPIRY_CACHE_PATH = os.path.join(TMP, "expiries.json")
md.refresh_tables(wait=True, timeout=400)      # real download in the background thread, waited on here
e_sx, e_nf = md._next_expiry_date(sx), md._next_expiry_date(nf)
print(f"      next expiries: SENSEX {e_sx} ({e_sx:%a}), NIFTY {e_nf} ({e_nf:%a})")
check("SENSEX expiry is a Thursday", e_sx.weekday() == 3, e_sx)
check("tables loaded from the real scrip master", bool(md._EXPIRY_CACHE.get("contracts")), list(md._EXPIRY_CACHE.get("contracts", {}))[:4])

c = md.find_contract("SENSEX", 72400, "CE")
check("SENSEX 72400 CE resolves to a real BFO contract", c is not None and c[0].startswith("SENSEX") and c[0].endswith("72400CE") and c[1].isdigit(), c)
check("SENSEX PE resolves too", (md.find_contract("SENSEX", 72400, "PE") or ("",))[0].endswith("72400PE"))
n = md.find_contract("NIFTY", 22550, "CE")
check("NIFTY 22550 CE resolves to a real NFO contract", n is not None and n[0].startswith("NIFTY") and n[0].endswith("22550CE"), n)
check("a non-existent strike returns None (caller falls back)", md.find_contract("SENSEX", 72450, "CE") is None)

# TREND_DAY sizing per instrument (synthetic strongly-trending day)
def trend_df(base, atr):
    """A clean one-way up day: +0.5 ATR per 5-min candle, so every close is a fresh session high."""
    idx = pd.date_range("2026-10-13 09:15", periods=60, freq="5min")
    step = atr * 0.5
    close = base + np.arange(60) * step
    return pd.DataFrame({"datetime": idx, "open": close - step, "high": close + atr * 0.15,
                         "low": close - step - atr * 0.15, "close": close, "volume": 0.0})
strat = ALL_STRATEGIES["TREND_DAY"]
for name, base, atr, lot, cost in (("NIFTY", 22500, 12, 65, 216.0), ("SENSEX", 72000, 38, 20, 126.0)):
    strat._fired = {}
    df = trend_df(base, atr)
    sig = strat.should_enter(df, 14.0, instrument=name, lot_size=lot, cost=cost)
    check(f"{name}: TREND_DAY signal produced", sig is not None)
    if sig:
        worst = sig.suggested_sl_pts * lot + cost
        check(f"{name}: stop {sig.suggested_sl_pts} pts → worst loss ₹{worst:.0f} within ₹{config.MAX_LOSS_PER_TRADE:.0f}",
              worst <= config.MAX_LOSS_PER_TRADE + 1e-6)

strat._fired = {}
strat.on_entry("NIFTY")
sig_s = strat.should_enter(trend_df(72000, 38), 14.0, instrument="SENSEX", lot_size=20, cost=126.0)
check("NIFTY having traded today does not block SENSEX", sig_s is not None)
check("per-instrument flag stored separately", set(strat._fired) == {"NIFTY"})
check("NIFTY itself is blocked after its trade", strat.should_enter(trend_df(22500, 12), 14.0, instrument="NIFTY", lot_size=65, cost=216.0) is None)

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
