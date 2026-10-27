"""research/fetch_index.py NAME EXCHANGE TOKEN — download ~99 days of 5-minute candles for any index."""
import os, sys, time
from datetime import datetime, timedelta
import pandas as pd
sys.path.insert(0, "/app")
from session_manager import SessionManager
name, exch, token = sys.argv[1:4]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"); os.makedirs(OUT, exist_ok=True)
smart = SessionManager.get().connect()
print("logged in; waiting 20s", flush=True); time.sleep(20)
now = datetime.now()
p = {"exchange": exch, "symboltoken": token, "interval": "FIVE_MINUTE",
     "fromdate": (now - timedelta(days=99)).strftime("%Y-%m-%d 09:15"), "todate": now.strftime("%Y-%m-%d %H:%M")}
for attempt in range(1, 6):
    try:
        r = smart.getCandleData(p)
        if r.get("status") and r.get("data"):
            df = pd.DataFrame(r["data"], columns=["datetime", "open", "high", "low", "close", "volume"])
            df.to_csv(os.path.join(OUT, f"{name.lower()}_FIVE_MINUTE.csv"), index=False)
            print(f"saved {len(df)} rows {df.datetime.iloc[0]} -> {df.datetime.iloc[-1]} volume_nonzero={(pd.to_numeric(df.volume)>0).sum()}", flush=True)
            break
        print(f"attempt {attempt}: {str(r)[:200]}", flush=True)
    except Exception as e:
        print(f"attempt {attempt} error: {str(e)[:160]}", flush=True)
    time.sleep(70)
print("done", flush=True); os._exit(0)
