"""
research/test_candles.py — offline tests for candle_store.CandleStore (no network).

  1. ticks → correct OHLC for each 5-min candle; the forming candle is never returned
  2. candles survive a restart (saved to disk, reloaded)
  3. official API candles overwrite local ones; API tz-aware times + forming bar handled
  4. API failure never blocks: decisions still get candles built from ticks
  5. api_due(): fills gaps, rate-limits its own retries, reconciles only periodically

(How close tick-built candles are to the exchange's is measured LIVE: every reconciliation
logs "local vs official candles … err" — a replay of real candles here would prove nothing.)
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pandas as pd

TMP = tempfile.mkdtemp(prefix="candles_test_")
sys.path.insert(0, "/app")
os.environ.setdefault("API_KEY", "x"); os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("PASSWORD", "x"); os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
import config                                   # noqa: E402
config.DATA_DIR = TMP
from candle_store import CandleStore, MIN_HISTORY_BARS   # noqa: E402

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)

t0 = datetime(2026, 10, 12, 10, 0, 0)

# 1) ticks → OHLC
s = CandleStore("TEST")
for sec, px in [(2, 100), (30, 103), (95, 99), (250, 101), (299, 102)]:       # candle 10:00
    s.add_tick(t0 + timedelta(seconds=sec), px)
for sec, px in [(301, 104), (400, 108), (580, 107)]:                          # candle 10:05
    s.add_tick(t0 + timedelta(seconds=sec), px)
s.add_tick(t0 + timedelta(seconds=605), 110)                                   # first tick of 10:10 → closes 10:05
df = s.closed(datetime(2026, 10, 12, 10, 5))
check("two completed candles returned", len(df) == 2, len(df))
r0, r1 = df.iloc[0], df.iloc[1]
check("candle 10:00 OHLC", (r0.open, r0.high, r0.low, r0.close) == (100, 103, 99, 102),
      (r0.open, r0.high, r0.low, r0.close))
check("candle 10:05 OHLC", (r1.open, r1.high, r1.low, r1.close) == (104, 108, 104, 107))
check("forming candle (10:10) not returned", df["datetime"].max() == datetime(2026, 10, 12, 10, 5))

# 2) restart → reload from disk
s2 = CandleStore("TEST")
check("candles reloaded after restart", len(s2._df) == 2 and os.path.exists(s2.path), len(s2._df))

# 3) API candles overwrite local; tz-aware + forming bar tolerated
api = pd.DataFrame({
    "datetime": pd.to_datetime(["2026-10-12T10:00:00+05:30", "2026-10-12T10:05:00+05:30", "2026-10-12T10:10:00+05:30"]),
    "open": [100.0, 104.5, 109.0], "high": [104.0, 109.0, 111.0], "low": [98.5, 103.5, 108.0],
    "close": [102.5, 108.5, 110.0], "volume": [0.0, 0.0, 0.0]})
n = s2.merge_api(api, datetime(2026, 10, 12, 10, 5))
d2 = s2.closed(datetime(2026, 10, 12, 10, 5))
check("API merge stores only closed candles", n == 2 and len(d2) == 2, n)
check("API candle overwrote local one", d2.iloc[0].high == 104.0 and d2.iloc[1].close == 108.5)
s2._finalize({"start": datetime(2026, 10, 12, 10, 0), "open": 1, "high": 9, "low": 0, "close": 5, "n": 9})
check("local candle never overwrites an official one", s2.closed(datetime(2026, 10, 12, 10, 5)).iloc[0].high == 104.0)

# 4) API down → local candles still available for a decision
s3 = CandleStore("DOWN")
base = datetime(2026, 10, 12, 11, 0, 0)
for k in range(8):                                                             # 8 bars built purely from ticks
    for sec in range(0, 300, 5):
        s3.add_tick(base + timedelta(minutes=5 * k, seconds=sec), 22400 + k + (sec % 20) / 10)
last = base + timedelta(minutes=5 * 7)
check("decision candles available with API down", len(s3.closed(last)) >= 7, len(s3.closed(last)))

# 5) api_due policy
s4 = CandleStore("DUE")
bar = datetime(2026, 10, 12, 12, 0)
check("fresh store (no history) wants the API", s4.api_due(bar))
s4.note_api(False)
check("does not retry the API immediately after a failure", not s4.api_due(bar))
s4._last_api_try = time.monotonic() - config.CANDLE_API_RETRY_SECS - 1
check("retries after the back-off", s4.api_due(bar))
idx = [t for t in pd.date_range(datetime(2026, 10, 8, 9, 15), datetime(2026, 10, 12, 12, 0), freq="5min")
       if (t.hour, t.minute) >= (9, 15) and (t.hour, t.minute) <= (15, 25) and t.weekday() < 5]
full = pd.DataFrame({"datetime": idx, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0.0})
s4.merge_api(full, bar)
s4._last_api_try = -1e9
check("no gap → no API call needed", not s4.has_gap(bar) and not s4.api_due(bar),
      f"bars={len(s4._df)} (need >= {MIN_HISTORY_BARS})")
s4._last_api_ok = time.monotonic() - config.CANDLE_RECONCILE_MINUTES * 60 - 5
check("periodic reconciliation becomes due", s4.api_due(bar))
s4._last_api_ok = time.monotonic()
s4._df = s4._df[s4._df["datetime"] != datetime(2026, 10, 12, 11, 30)].reset_index(drop=True)
check("a missing candle is detected as a gap", s4.has_gap(bar))

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
