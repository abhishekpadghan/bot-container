"""Replay the deployed TrendDayStrategy over the real history and compare with the lab's signals."""
import os, sys
sys.path.insert(0, "/app")
os.environ.setdefault("API_KEY", "x"); os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("PASSWORD", "x"); os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
import pandas as pd
import strategy_lab as lab
from strategies import TrendDayStrategy

hist = lab.add_indicators(lab.load_5m())
lab_sig = lab.sig_trend_day(hist)
lab_days = {}
for i in hist.index[(lab_sig != 0) & (hist["tod"] <= 14 * 60 + 15)]:
    lab_days.setdefault(hist.at[i, "date"], (hist.at[i, "datetime"], int(lab_sig[i])))

strat = TrendDayStrategy()
live_days = {}
for d, day in hist.groupby("date"):
    for i in day.index:
        if hist.at[i, "tod"] > 14 * 60 + 15:
            break
        window = hist.loc[max(0, i - 374): i, ["datetime", "open", "high", "low", "close", "volume"]]
        strat._fired = {}
        s = strat.should_enter(window, 14.0)
        if s:
            live_days[d] = (hist.at[i, "datetime"], 1 if s.option_type.value == "CE" else -1, s.suggested_sl_pts)
            break
print(f"lab: {len(lab_days)} trend-day signals | deployed strategy: {len(live_days)}")
same = [d for d in lab_days if d in live_days and lab_days[d][0] == live_days[d][0] and lab_days[d][1] == live_days[d][1]]
print(f"identical (same day, bar, direction): {len(same)}")
for d in sorted(set(lab_days) ^ set(live_days)):
    print("  differs:", d, "lab", lab_days.get(d), "live", live_days.get(d))
print("sample stops (premium pts):", [v[2] for v in list(live_days.values())[:8]])
