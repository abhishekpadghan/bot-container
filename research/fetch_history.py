"""
research/fetch_history.py — one-off download of real Nifty index candles for
strategy research. Writes CSVs to research/data/.  Run via:

    podman-compose --profile tools run --rm cli research/fetch_history.py

Uses one getCandleData call per interval, spaced well apart to respect
Angel One's strict historical-data quota.
"""
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, "/app")
from session_manager import SessionManager   # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)

# (interval, max days Angel One allows per request)
JOBS = [("FIVE_MINUTE", 99), ("FIFTEEN_MINUTE", 199), ("ONE_DAY", 400)]

sm = SessionManager.get()
smart = sm.connect()
print("logged in; waiting 20s before first historical call", flush=True)
time.sleep(20)

for interval, days in JOBS:
    now = datetime.now()
    params = {
        "exchange": "NSE", "symboltoken": "99926000", "interval": interval,
        "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    for attempt in range(1, 4):
        try:
            resp = smart.getCandleData(params)
            if resp.get("status") and resp.get("data"):
                df = pd.DataFrame(resp["data"],
                                  columns=["datetime", "open", "high", "low", "close", "volume"])
                path = os.path.join(OUT, f"nifty_{interval}.csv")
                df.to_csv(path, index=False)
                print(f"{interval}: {len(df)} rows {df.datetime.iloc[0]} -> {df.datetime.iloc[-1]} "
                      f"volume_nonzero={(pd.to_numeric(df.volume) > 0).sum()}", flush=True)
                break
            print(f"{interval}: attempt {attempt} bad response: {str(resp)[:200]}", flush=True)
        except Exception as exc:
            print(f"{interval}: attempt {attempt} error: {str(exc)[:200]}", flush=True)
        time.sleep(65)
    time.sleep(65)   # stay under the ~1 historical call/min quota

print("done", flush=True)
os._exit(0)   # don't wait on the heartbeat thread
