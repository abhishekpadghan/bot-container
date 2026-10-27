"""TREND_DAY with the trend measured from the PREVIOUS CLOSE (gap-aware) vs from the day's open.
Deployed exits: stop 16.4 idx pts (~8.2 premium), trail after +1R by 0.5R, no fixed target."""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab

def sig_trend_day_ref(df, ref="open", min_move=0.004):
    ref_px = df["day_open"] if ref == "open" else df["pdc"]
    mv = (df["close"] - ref_px) / ref_px
    hi = df.groupby("date")["high"].transform(lambda s: s.shift(1).expanding().max())
    lo = df.groupby("date")["low"].transform(lambda s: s.shift(1).expanding().min())
    up = (mv >= min_move) & (df["close"] > df["vwap"]) & (df["ema9"] > df["ema21"]) & (df["close"] > hi)
    dn = (mv <= -min_move) & (df["close"] < df["vwap"]) & (df["ema9"] < df["ema21"]) & (df["close"] < lo)
    s = pd.Series(np.where(up & (df["bar"] >= 12), 1, np.where(dn & (df["bar"] >= 12), -1, 0)), index=df.index)
    first = s.ne(0).groupby(df["date"]).cumsum().eq(1) & s.ne(0)
    return s.where(first, 0)

df = lab.add_indicators(lab.load_5m())
dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
kw = dict(fixed_sl=16.4, fixed_tgt=None, trail_at_idx=16.4, trail_dist_idx=8.2, t_end=14 * 60 + 15)
print(f"{len(dates)} days, test starts {cut}\n")
for ref in ("open", "pdc"):
    for mm in (0.003, 0.004, 0.006):
        sg = sig_trend_day_ref(df, ref, mm)
        r = {t: lab.stats(lab.run(df[m], sg[m], 0, 0, **kw)) for t, m in
             (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL", df["date"] >= dates[0]))}
        print(f"ref={'day open' if ref == 'open' else 'prev close':<10} min_move={mm*100:.1f}%  " + " | ".join(
            f"{k} n={v['n']} net=₹{v['net']:,} PF={v['pf']}" for k, v in r.items()))
