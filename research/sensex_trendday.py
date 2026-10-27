"""
research/sensex_trendday.py — does the deployed TREND_DAY rule behave on SENSEX?

Same rule and exits as the bot (ATR stop capped so one stopped trade fits MAX_LOSS_PER_TRADE,
trail after +1R by 0.5R, no fixed target), but with SENSEX's lot (20), assumed cost (₹126),
Thursday expiry and index level. NIFTY is re-run beside it for comparison.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_lab as lab   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_LOSS = 750.0


def load(name):
    df = pd.read_csv(os.path.join(HERE, "data", f"{name}_FIVE_MINUTE.csv"))
    df["datetime"] = pd.to_datetime(df["datetime"].str[:19])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.date
    df["tod"] = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
    df["bar"] = df.groupby("date").cumcount()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c])
    return df


def evaluate(name, lot, cost, expiry_wd):
    lab.LOT, lab.COST = lot, cost
    lab.days_to_expiry = lambda d: ((expiry_wd - d.weekday()) % 7) or 7
    df = lab.add_indicators(load(name))
    dates = sorted(df["date"].unique())
    cut = dates[int(len(dates) * 2 / 3)]
    px = float(df["close"].median())
    cap_idx = (MAX_LOSS - cost) / lot / lab.DELTA            # stop cap in INDEX points
    floor_idx = 0.00035 * px
    print(f"\n=== {name}: {len(dates)} days {dates[0]} -> {dates[-1]} | lot {lot}, cost ₹{cost:.0f}, "
          f"median level {px:,.0f}, avg 5m ATR {df['atr'].mean():.1f} pts ===")
    print(f"    stop = clamp(1 ATR, {floor_idx:.0f}, {cap_idx:.1f}) index pts (≈ premium {cap_idx*lab.DELTA:.1f} max), "
          f"trail after 1R by 0.5R")
    kw = dict(sl_atr=1.0, rr=None, trail_r=1.0, trail_d=0.5, sl_floor=floor_idx, sl_cap=cap_idx, t_end=14 * 60 + 15)
    for move in (0.004,):
        sig = lab.sig_trend_day(df, move)
        for tag, m in (("TRAIN", df["date"] < cut), ("TEST", df["date"] >= cut), ("ALL", df["date"] >= dates[0])):
            print(lab.fmt(tag, lab.stats(lab.run(df[m], sig[m], **kw))))
        n_days = sig[sig != 0].groupby(df["date"]).size().shape[0]
        print(f"    signal days: {n_days}/{len(dates)}")
    # friction check: random entries with the same exits
    rng = np.random.default_rng(5)
    rs = pd.Series(np.where(rng.random(len(df)) < 0.03, rng.choice([-1, 1], len(df)), 0), index=df.index)
    print(lab.fmt("RANDOM", lab.stats(lab.run(df, rs, **kw))) + "   <- no-edge baseline with the same exits")


if __name__ == "__main__":
    evaluate("sensex", 20, 126.0, 3)
    evaluate("nifty", 65, 216.0, 1)
