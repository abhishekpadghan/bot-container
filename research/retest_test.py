"""
research/retest_test.py — "break → retest → rejection" (the pattern both indices showed on 14 Oct at 13:00).

Bearish version (bullish is the mirror):
  1. after 10:15, a candle CLOSES below the session low so far (by 0.1 ATR) and the day is already
     down >= 0.3% from its open  → that old low becomes resistance ("the broken level"),
  2. within the next 3–24 candles price bounces back to within 0.25 ATR of the broken level
     (without closing back above it by more than 0.3 ATR),
  3. a bearish candle then closes below the level → buy a PUT at the next open.
Stop = just above the retest high (structure stop, clamped to the bot's ₹750 loss cap);
exits: trail after +1R by 0.5R (deployed) — no fixed target. Once per break.
"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab
import sensex_trendday as sx


def retest_signals(df, min_move=0.003, first_bar=12, lo_win=(3, 24)):
    sig = pd.Series(0, index=df.index)
    stop = pd.Series(np.nan, index=df.index)
    for d, day in df.groupby("date", sort=True):
        h, l, c, o, atr = (day[k].values for k in ("high", "low", "close", "open", "atr"))
        idx = day.index.values
        n, day_open = len(day), o[0]
        i, done = first_bar, False
        sess_lo = np.minimum.accumulate(l); sess_hi = np.maximum.accumulate(h)
        while i < n - 1 and not done:
            prev_lo, prev_hi = sess_lo[i - 1], sess_hi[i - 1]
            for side in (-1, +1):
                if side < 0:
                    broke = c[i] < prev_lo - 0.1 * atr[i] and (c[i] - day_open) / day_open <= -min_move
                    level = prev_lo
                else:
                    broke = c[i] > prev_hi + 0.1 * atr[i] and (c[i] - day_open) / day_open >= min_move
                    level = prev_hi
                if not broke:
                    continue
                extreme = h[i] if side < 0 else l[i]          # furthest retest excursion so far
                for j in range(i + lo_win[0], min(i + lo_win[1], n - 1) + 1):
                    a = atr[j]
                    # invalidated if price reclaims the level decisively
                    if (side < 0 and c[j] > level + 0.3 * a) or (side > 0 and c[j] < level - 0.3 * a):
                        break
                    extreme = max(extreme, h[j]) if side < 0 else min(extreme, l[j])
                    touched = (h[j] >= level - 0.25 * a) if side < 0 else (l[j] <= level + 0.25 * a)
                    reject = (c[j] < level and c[j] < o[j]) if side < 0 else (c[j] > level and c[j] > o[j])
                    if touched and reject:
                        sig.at[idx[j]] = side
                        stop.at[idx[j]] = abs(extreme - c[j]) + 0.1 * a
                        done = True
                        break
                if done:
                    break
            i += 1
    return sig, stop


for name, lot, cost, wd in (("nifty", 65, 216.0, 1), ("sensex", 20, 126.0, 3)):
    lab.LOT, lab.COST = lot, cost
    lab.days_to_expiry = lambda d, wd=wd: ((wd - d.weekday()) % 7) or 7
    df = lab.add_indicators(sx.load(name))
    dates = sorted(df["date"].unique()); cut = dates[int(len(dates) * 2 / 3)]
    px = float(df["close"].median()); cap = (750 - cost) / lot / lab.DELTA
    sig, stop = retest_signals(df)
    n_days = sig[sig != 0].groupby(df["date"]).size().shape[0]
    print(f"\n=== {name.upper()}: {len(dates)} days | retest signals on {n_days} days | stop = structure, clamp({0.00035*px:.0f}, {cap:.1f}) idx pts ===")
    for label, tr, td in (("deployed trail 1R/0.5R", 1.0, 0.5), ("trail 1R/1R", 1.0, 1.0), ("trail 1.5R/1R", 1.5, 1.0)):
        kw = dict(sl_atr=1.0, rr=None, trail_r=tr, trail_d=td, sl_floor=0.00035 * px, sl_cap=cap, sl_arr=stop, t_end=14 * 60 + 15)
        r = {t: lab.stats(lab.run(df[m], sig[m], **{**kw, "sl_arr": stop[m]})) for t, m in
             (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL", df["date"] >= dates[0]))}
        print(f"  {label:<22} " + " | ".join(f"{k} n={v['n']:<3} net=₹{v['net']:>7,} PF={v['pf']}" for k, v in r.items()))
    # was today's setup recognised?
    today = df[df["date"] == dates[-1]]
