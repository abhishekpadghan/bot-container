"""Sanity check of strategy_lab's simulator using RANDOM entries (no edge by construction)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab   # noqa: E402

df = lab.add_indicators(lab.load_5m())
rng = np.random.default_rng(7)
sig = pd.Series(np.where(rng.random(len(df)) < 0.03, rng.choice([-1, 1], len(df)), 0), index=df.index)


def go(label, **kw):
    t = lab.run(df, sig, sl_atr=1.5, rr=2.0, trail_r=None, **kw)
    s = lab.stats(t)
    gross = (t.pnl + lab.COST).mean() if len(t) else 0
    print(f"{label:<44} n={s['n']:>4} win%={s['win%']:>5} avg net=₹{s['avg']:>6} (avg before cost=₹{gross:,.0f})")


go("random entries, full model (cost + theta)")
lab.COST = 0.0
go("random entries, cost=0")
lab.BARS_PER_DAY = 10 ** 9            # makes theta ~0
go("random entries, cost=0 and theta=0  (expect ~0)")
