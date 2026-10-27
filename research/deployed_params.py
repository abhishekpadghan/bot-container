"""Backtest the EXACT deployed TREND_DAY exit parameters (stop 16.4 idx pts ~ 8.2 premium, trail 1R/0.5R)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
df = lab.add_indicators(lab.load_5m())
sig = lab.sig_trend_day(df)
dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
kw = dict(fixed_sl=16.4, fixed_tgt=None, trail_at_idx=16.4, trail_dist_idx=8.2, t_end=14 * 60 + 15)
for tag, m in (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL 70d", df["date"] >= dates[0])):
    t = lab.run(df[m], sig[m], 0, 0, **kw)
    print(lab.fmt(tag, lab.stats(t)))
    if tag == "ALL 70d" and len(t):
        print("   exit reasons:", t["reason"].value_counts().to_dict())
        print("   avg winner ₹%.0f | avg loser ₹%.0f | best ₹%.0f | worst ₹%.0f" % (
            t.loc[t.pnl > 0, "pnl"].mean(), t.loc[t.pnl <= 0, "pnl"].mean(), t.pnl.max(), t.pnl.min()))
        t["month"] = t["date"].astype(str).str[:7]
        print("   by month:", t.groupby("month")["pnl"].agg(['count', 'sum']).round(0).to_dict('index'))
