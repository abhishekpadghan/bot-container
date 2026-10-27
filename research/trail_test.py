"""
research/trail_test.py — (1) how wide should the trailing stop be? (2) what does the daily-target lock cost?
5-minute candles, 70 days, deployed entry rule, per instrument. Stop = ATR clamped to the ₹750 cap.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
import sensex_trendday as sx

for name, lot, cost, wd in (("nifty", 65, 216.0, 1), ("sensex", 20, 126.0, 3)):
    lab.LOT, lab.COST = lot, cost
    lab.days_to_expiry = lambda d, wd=wd: ((wd - d.weekday()) % 7) or 7
    df = lab.add_indicators(sx.load(name))
    dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
    px = float(df["close"].median()); cap = (750 - cost) / lot / lab.DELTA
    sig = lab.sig_trend_day(df, 0.004)
    print(f"\n=== {name.upper()}: trail after +Ar by Dr behind the peak (r = stop size) — TRAIN | TEST | ALL net ₹ (PF) ===")
    for a, d in ((1.0, 0.5), (1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (2.0, 1.0), (2.0, 2.0), (3.0, 1.5)):
        kw = dict(sl_atr=1.0, rr=None, trail_r=a, trail_d=d, sl_floor=0.00035 * px, sl_cap=cap, t_end=14 * 60 + 15)
        r = {t: lab.stats(lab.run(df[m], sig[m], **kw)) for t, m in
             (("TR", df["date"] < cut), ("TE", df["date"] >= cut), ("ALL", df["date"] >= dates[0]))}
        mark = "  <- deployed" if (a, d) == (1.0, 0.5) else ""
        print(f"  trail {a}R / {d}R: " + " | ".join(f"{k} {v['net']:>7,} ({v['pf']})" for k, v in r.items()) + f"  n={r['ALL']['n']}{mark}")
    print(f"  --- daily +₹1500 target lock (deployed trail) ---")
    kw = dict(sl_atr=1.0, rr=None, trail_r=1.0, trail_d=0.5, sl_floor=0.00035 * px, sl_cap=cap, t_end=14 * 60 + 15)
    for tgt in (1500.0, 1e9):
        lab.DAY_TARGET = tgt
        st = lab.stats(lab.run(df, sig, **kw))
        print(f"  daily target {'₹1500' if tgt < 1e8 else 'none  '}: n={st['n']} net=₹{st['net']:,} PF={st['pf']}")
    lab.DAY_TARGET = 1500.0
