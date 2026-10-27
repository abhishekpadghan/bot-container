"""Does starting TREND_DAY earlier than 10:15 help? (bars: 3=09:30, 6=09:45, 9=10:00, 12=10:15)
Deployed exits, both indices, TRAIN/TEST split. Expiry-afternoon block is NOT applied (the bot trades next week's contract)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
import sensex_trendday as sx

for name, lot, cost, wd in (("nifty", 65, 216.0, 1), ("sensex", 20, 126.0, 3)):
    lab.LOT, lab.COST = lot, cost
    lab.days_to_expiry = lambda d, wd=wd: ((wd - d.weekday()) % 7) or 7
    df = lab.add_indicators(sx.load(name))
    dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
    px = float(df["close"].median())
    cap = (750 - cost) / lot / lab.DELTA
    kw = dict(sl_atr=1.0, rr=None, trail_r=1.0, trail_d=0.5, sl_floor=0.00035 * px, sl_cap=cap, t_end=14 * 60 + 15)
    print(f"\n=== {name.upper()} ({len(dates)} days) ===")
    for fb in (3, 6, 9, 12):
        sig = lab.sig_trend_day(df, 0.004, first_bar=fb)
        r = {t: lab.stats(lab.run(df[m], sig[m], **kw)) for t, m in
             (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL", df["date"] >= dates[0]))}
        print(f"  start {9 + (15 + 5 * fb) // 60}:{(15 + 5 * fb) % 60:02d}  " + " | ".join(
            f"{k} n={v['n']:<3} net=₹{v['net']:>7,} PF={v['pf']}" for k, v in r.items()))
