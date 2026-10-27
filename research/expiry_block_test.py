"""What does the 'no entries after 13:00 on expiry day' block cost/save? (the bot never buys the expiring contract)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import strategy_lab as lab
import sensex_trendday as sx

for name, lot, cost, wd in (("nifty", 65, 216.0, 1), ("sensex", 20, 126.0, 3)):
    lab.LOT, lab.COST = lot, cost
    lab.days_to_expiry = lambda d, wd=wd: ((wd - d.weekday()) % 7) or 7
    df = lab.add_indicators(sx.load(name))
    px = float(df["close"].median()); cap = (750 - cost) / lot / lab.DELTA
    kw = dict(sl_atr=1.0, rr=None, trail_r=1.0, trail_d=0.5, sl_floor=0.00035 * px, sl_cap=cap, t_end=14 * 60 + 15)
    sig = lab.sig_trend_day(df, 0.004)
    blocked = sig.where(~((df["datetime"].dt.weekday == wd) & (df["tod"] >= 13 * 60)), 0)
    a, b = lab.run(df, sig, **kw), lab.run(df, blocked, **kw)
    sa, sb = lab.stats(a), lab.stats(b)
    on_exp = a[a["date"].map(lambda d: d.weekday() == wd)]
    print(f"\n{name.upper()}: without block  n={sa['n']} net=₹{sa['net']:,} PF={sa['pf']}")
    print(f"       with block     n={sb['n']} net=₹{sb['net']:,} PF={sb['pf']}")
    print(f"       trades the block removes: {sa['n']-sb['n']}  (their net ₹{sa['net']-sb['net']:,})")
