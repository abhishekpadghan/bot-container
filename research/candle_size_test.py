"""
research/candle_size_test.py — does deciding on 2- or 3-minute candles beat 5-minute for TREND_DAY?

Candles are rebuilt from the real 1-minute history (Aug 17 – Oct 13, ~40 days). Two ways to port the rule:
  "same bars"   : identical bar counts (EMA 9/21, ATR 14, start after 12 bars) — shorter bars => shorter look-backs
  "same clock"  : start after 60 minutes of trading (12 five-min bars = 20 three-min = 30 two-min)
Deployed exits (ATR stop clamped to the ₹750 loss cap, trail after 1R by 0.5R, no target); NIFTY lot 65 / cost ₹216.
"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab

HERE = os.path.dirname(os.path.abspath(__file__))

def candles(minutes):
    m = pd.read_csv(os.path.join(HERE, "data", "nifty_ONE_MINUTE.csv"))
    m["datetime"] = pd.to_datetime(m["datetime"].str[:19])
    m = m.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    m["date"] = m["datetime"].dt.date
    for c in ("open", "high", "low", "close", "volume"):
        m[c] = pd.to_numeric(m[c])
    m["grp"] = m.groupby("date").cumcount() // minutes
    out = m.groupby(["date", "grp"]).agg(datetime=("datetime", "first"), open=("open", "first"), high=("high", "max"),
                                         low=("low", "min"), close=("close", "last"), volume=("volume", "sum")).reset_index()
    out["tod"] = out["datetime"].dt.hour * 60 + out["datetime"].dt.minute
    out["bar"] = out["grp"]
    return out.drop(columns="grp").sort_values("datetime").reset_index(drop=True)

lab.LOT, lab.COST = 65, 216.0
lab.days_to_expiry = lambda d: ((1 - d.weekday()) % 7) or 7
cap = (750 - 216.0) / 65 / lab.DELTA
print(f"stop cap {cap:.1f} index pts; theta/premium model per bar scaled by bars/day\n")
for minutes in (5, 3, 2):
    lab.BARS_PER_DAY = 375 / minutes
    df = lab.add_indicators(candles(minutes))
    dates = sorted(df["date"].unique())
    px = float(df["close"].median())
    kw = dict(sl_atr=1.0, rr=None, trail_r=1.0, trail_d=0.5, sl_floor=0.00035 * px, sl_cap=cap, t_end=14 * 60 + 15)
    for label, fb in (("same bars (12)", 12), ("same clock (60 min)", 60 // minutes)):
        if minutes == 5 and label.startswith("same clock"):
            continue
        sig = lab.sig_trend_day(df, 0.004, first_bar=fb)
        st = lab.stats(lab.run(df, sig, **kw))
        print(f"{minutes}-min candles, {label:<20} n={st['n']:<3} win%={st['win%']:<5} net=₹{st['net']:>7,} PF={st['pf']:<5} maxDD=₹{st['maxdd']:,}  (avg ATR {df['atr'].mean():.1f} pts, {len(dates)} days)")
