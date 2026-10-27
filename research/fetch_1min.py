"""research/fetch_1min.py — one-off download of ~58 calendar days of 1-minute Nifty index candles
(two requests of <=29 days, spaced to respect the historical-data quota)."""
import os, sys, time
from datetime import datetime, timedelta
import pandas as pd
sys.path.insert(0, "/app")
from session_manager import SessionManager

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"); os.makedirs(OUT, exist_ok=True)
smart = SessionManager.get().connect()
print("logged in; waiting 20s", flush=True); time.sleep(20)
now = datetime.now(); frames = []
CHUNKS = [(29, 0)] if (len(sys.argv) > 1 and sys.argv[1] == 'recent') else [(29, 0), (58, 29)]
for k, (a, b) in enumerate(CHUNKS):
    p = {"exchange": "NSE", "symboltoken": "99926000", "interval": "ONE_MINUTE",
         "fromdate": (now - timedelta(days=a)).strftime("%Y-%m-%d 09:15"),
         "todate": (now - timedelta(days=b)).strftime("%Y-%m-%d %H:%M")}
    for attempt in range(1, 5):
        try:
            r = smart.getCandleData(p)
            if r.get("status") and r.get("data"):
                frames.append(pd.DataFrame(r["data"], columns=["datetime", "open", "high", "low", "close", "volume"]))
                print(f"chunk {k}: {len(r['data'])} rows {r['data'][0][0]} -> {r['data'][-1][0]}", flush=True); break
            print(f"chunk {k} attempt {attempt}: {str(r)[:160]}", flush=True)
        except Exception as e:
            print(f"chunk {k} attempt {attempt} error: {str(e)[:160]}", flush=True)
        time.sleep(65)
    time.sleep(65)
if frames:
    path = os.path.join(OUT, "nifty_ONE_MINUTE.csv")
    if os.path.exists(path):
        frames.append(pd.read_csv(path))            # keep what we already have
    df = pd.concat(frames).drop_duplicates("datetime").sort_values("datetime")
    df.to_csv(path, index=False)
    print("saved", len(df), "rows", df.datetime.iloc[0], "->", df.datetime.iloc[-1], flush=True)
print("done", flush=True); os._exit(0)
