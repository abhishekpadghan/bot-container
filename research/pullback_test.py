"""
research/pullback_test.py — does "buy the pullback inside an established trend day" work?

Rule (bearish version; bullish is the mirror):
  * the day has trended down >= MIN_MOVE from its reference price at some point
    (reference = day's open, or previous close so GAP days count),
  * trend still intact: close < session average, EMA9 < EMA21,
  * price bounced up toward EMA21 within the last 8 candles,
  * the just-closed candle rejects: closes below EMA9 AND below the prior candle's low.
=> buy a PUT.  Exits are tested with the same ATR-stop / trail grid as strategy_lab.py.
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab   # noqa: E402


def sig_trend_pullback(df, ref="open", min_move=0.004, once_per_day=False):
    ref_px = df["day_open"] if ref == "open" else df["pdc"]
    lo_x = df.groupby("date")["low"].transform(lambda s: s.expanding().min())
    hi_x = df.groupby("date")["high"].transform(lambda s: s.expanding().max())
    down_day = (lo_x - ref_px) / ref_px <= -min_move
    up_day = (hi_x - ref_px) / ref_px >= min_move
    bear = down_day & (df["close"] < df["vwap"]) & (df["ema9"] < df["ema21"]) \
        & (df["high"].rolling(8).max() >= df["ema21"]) \
        & (df["close"] < df["ema9"]) & (df["close"] < df["low"].shift(1)) & df["rsi"].between(30, 62)
    bull = up_day & (df["close"] > df["vwap"]) & (df["ema9"] > df["ema21"]) \
        & (df["low"].rolling(8).min() <= df["ema21"]) \
        & (df["close"] > df["ema9"]) & (df["close"] > df["high"].shift(1)) & df["rsi"].between(38, 70)
    s = pd.Series(np.where((df["bar"] >= 6) & bull, 1, np.where((df["bar"] >= 6) & bear, -1, 0)), index=df.index)
    if once_per_day:
        first = s.ne(0).groupby(df["date"]).cumsum().eq(1) & s.ne(0)
        s = s.where(first, 0)
    return s


def main():
    df = lab.add_indicators(lab.load_5m())
    dates = sorted(df["date"].unique())
    cut = dates[int(len(dates) * 2 / 3)]
    tr, te = df["date"] < cut, df["date"] >= cut
    grid = [dict(sl_atr=a, rr=r, trail_r=t, trail_d=d)
            for a, r, (t, d) in itertools.product(
                [1.0, 1.5, 2.0, 3.0], [None, 2.0, 3.0, 4.0],
                [(None, 0.5), (1.0, 0.5), (1.5, 1.0), (2.0, 1.0)])
            if not (r is None and t is None)]

    print(f"{len(dates)} days | TRAIN < {cut} <= TEST | cost ₹{lab.COST:.0f}/trade | random-entry baseline ≈ -₹480/trade\n")
    for ref in ("open", "pdc"):
        for once in (False, True):
            sig = sig_trend_pullback(df, ref=ref, once_per_day=once)
            n_days = sig[sig != 0].groupby(df["date"]).size().shape[0]
            best = None
            for p in grid:
                st = lab.stats(lab.run(df[tr], sig[tr], **p))
                if st["n"] >= 15 and (best is None or st["net"] > best[0]["net"]):
                    best = (st, p)
            tag = f"ref={'day open' if ref == 'open' else 'prev close (gap-aware)'}, {'1 trade/day' if once else 'multiple/day'}"
            print(f"▶ {tag}   (signal days: {n_days}/{len(dates)})")
            if best is None:
                print("   too few trades\n")
                continue
            p = best[1]
            print(f"   best-on-train params: {p}")
            print(lab.fmt("TRAIN", best[0]))
            print(lab.fmt("TEST", lab.stats(lab.run(df[te], sig[te], **p))))
            print(lab.fmt("ALL", lab.stats(lab.run(df, sig, **p))) + "\n")


if __name__ == "__main__":
    main()
