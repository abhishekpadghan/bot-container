"""Gap-aware trend-pullback with the stop capped the way the bot caps it (16.4 idx pts ≈ 8.2 premium)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
from pullback_test import sig_trend_pullback
df = lab.add_indicators(lab.load_5m())
dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
for once in (True, False):
    sig = sig_trend_pullback(df, ref="pdc", once_per_day=once)
    print(f"\n== gap-aware pullback, {'1 trade/day' if once else 'multiple/day'}, stop 16.4 idx pts ==")
    for rr in (None, 2.0, 3.0, 4.0):
        kw = dict(fixed_sl=16.4, fixed_tgt=(16.4 * rr if rr else None), trail_at_idx=16.4, trail_dist_idx=8.2, t_end=14*60+15)
        res = {tag: lab.stats(lab.run(df[m], sig[m], 0, 0, **kw))
               for tag, m in (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL", df["date"] >= dates[0]))}
        print(f"  target={'trail-only' if rr is None else f'{rr}R':<10}", " | ".join(
            f"{k} n={v['n']} net=₹{v['net']:,} PF={v['pf']}" for k, v in res.items()))
