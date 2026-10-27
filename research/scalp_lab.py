"""
research/scalp_lab.py — can SHORT-HOLD, SMALL-TARGET option scalps make money after costs?

Uses real 1-minute Nifty candles (research/data/nifty_ONE_MINUTE.csv). A bought ATM option is
modelled from the index move (delta 0.5, small theta), cost per round trip shown in rupees.
Signals use CLOSED 1-min bars; entry at the NEXT bar's open; stop is checked before target inside
a bar (pessimistic); positions are force-closed after `max_bars` minutes.

Run:  podman-compose --profile tools run --rm --no-deps cli research/scalp_lab.py
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import strategy_lab as lab   # noqa: E402  (stats / fmt helpers)

LOT, DELTA, SIGMA = 65, 0.5, 0.15
MAX_TRADES, DAY_STOP, DAY_TARGET = 5, -750.0, 1500.0
BARS_PER_DAY = 375


def load_1m() -> pd.DataFrame:
    df = pd.read_csv(os.path.join(HERE, "data", "nifty_ONE_MINUTE.csv"))
    df["datetime"] = pd.to_datetime(df["datetime"].str[:19])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.date
    df["tod"] = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
    df["bar"] = df.groupby("date").cumcount()
    return df


def add_ind(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    tp = (h + l + c) / 3
    df["vwap"] = tp.groupby(df["date"]).transform(lambda s: s.expanding().mean())
    df["rng"] = h - l
    df["pos"] = ((c - l) / (h - l).replace(0, np.nan)).fillna(0.5)
    df["hh"] = h.shift(1).rolling(10).max()
    df["ll"] = l.shift(1).rolling(10).min()

    # 5-minute trend, aligned so a 1-min bar only sees CLOSED 5-min bars
    m5 = (df.set_index("datetime").resample("5min")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    m5["e9"], m5["e21"] = m5["close"].ewm(span=9, adjust=False).mean(), m5["close"].ewm(span=21, adjust=False).mean()
    m5["avail"] = m5.index + pd.Timedelta(minutes=5)
    m5 = m5.reset_index(drop=True)[["avail", "e9", "e21"]]
    left = df[["datetime"]].copy()
    left["t_close"] = left["datetime"] + pd.Timedelta(minutes=1)
    mm = pd.merge_asof(left.sort_values("t_close"), m5, left_on="t_close", right_on="avail", direction="backward")
    df["e9_5"], df["e21_5"] = mm["e9"].values, mm["e21"].values
    return df


# ── signals (+1 buy CE, -1 buy PE) ────────────────────────────

def sig_burst(df, k=1.8):
    """Momentum burst: big-range bar closing at its extreme and beyond the last 10 bars, with the 5m trend."""
    big = df["rng"] >= k * df["atr"].shift(1)
    up = big & (df["pos"] >= 0.8) & (df["close"] > df["hh"]) & (df["e9_5"] > df["e21_5"]) & (df["close"] > df["vwap"])
    dn = big & (df["pos"] <= 0.2) & (df["close"] < df["ll"]) & (df["e9_5"] < df["e21_5"]) & (df["close"] < df["vwap"])
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=df.index)


def sig_micro_pullback(df):
    """5m uptrend; 3 lower 1-min closes (a dip); then a bar closing above the prior bar's high."""
    c = df["close"]
    dip_up = (c.shift(1) < c.shift(2)) & (c.shift(2) < c.shift(3)) & (c.shift(3) < c.shift(4))
    rise_dn = (c.shift(1) > c.shift(2)) & (c.shift(2) > c.shift(3)) & (c.shift(3) > c.shift(4))
    up = dip_up & (c > df["high"].shift(1)) & (df["e9_5"] > df["e21_5"]) & (c > df["vwap"])
    dn = rise_dn & (c < df["low"].shift(1)) & (df["e9_5"] < df["e21_5"]) & (c < df["vwap"])
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=df.index)


def sig_random(df, p=0.02, seed=3):
    rng = np.random.default_rng(seed)
    return pd.Series(np.where(rng.random(len(df)) < p, rng.choice([-1, 1], len(df)), 0), index=df.index)


# ── simulation ────────────────────────────────────────────────

def days_to_expiry(d) -> int:
    n = (1 - d.weekday()) % 7
    return n if n else 7


def run(df, sig, sl, tgt, max_bars, cost, t_start=9 * 60 + 30, t_end=14 * 60 + 15):
    out = []
    for d, day in df.groupby("date", sort=True):
        o, h, l, c = (day[k].values for k in ("open", "high", "low", "close"))
        tod, s = day["tod"].values, sig.loc[day.index].values
        n = len(day)
        last_ok = int(np.nonzero(tod <= 15 * 60 + 10)[0].max())
        t_days, pnl_day, trades, i = days_to_expiry(d), 0.0, 0, 0
        while i < n - 2 and trades < MAX_TRADES and DAY_STOP < pnl_day < DAY_TARGET:
            if s[i] != 0 and t_start <= tod[i] <= t_end:
                e, dirn, j = o[i + 1], int(s[i]), i + 1
                p0 = 0.4 * e * SIGMA * np.sqrt(max(t_days - day["bar"].values[j] / BARS_PER_DAY, 0.3) / 365)
                theta = p0 / (2 * max(t_days, 0.3) * BARS_PER_DAY)
                fav, k_exit, reason = None, None, "TIME"
                for k in range(j, min(j + max_bars, last_ok + 1)):
                    hi = (h[k] - e) if dirn > 0 else (e - l[k])
                    lo = (l[k] - e) if dirn > 0 else (e - h[k])
                    if lo <= -sl:
                        fav, k_exit, reason = -sl, k, "SL"; break
                    if hi >= tgt:
                        fav, k_exit, reason = tgt, k, "TGT"; break
                if fav is None:
                    k_exit = min(j + max_bars - 1, last_ok)
                    fav = (c[k_exit] - e) * dirn
                pnl = (DELTA * fav - theta * (k_exit - j + 1)) * LOT - cost
                out.append({"date": d, "pnl": pnl, "reason": reason})
                pnl_day += pnl; trades += 1; i = k_exit + 1
            else:
                i += 1
    return pd.DataFrame(out)


def main():
    df = add_ind(load_1m())
    dates = sorted(df["date"].unique())
    cut = dates[int(len(dates) * 2 / 3)]
    tr, te = df["date"] < cut, df["date"] >= cut
    print(f"{len(dates)} trading days {dates[0]} -> {dates[-1]} | TRAIN < {cut} <= TEST | avg 1-min ATR {df['atr'].mean():.1f} pts")
    print("Scalp = stop/target in INDEX points (premium moves ~half): 6 pts ≈ 3 premium pts ≈ ₹195; cost ₹216 ≈ 6.6 index pts\n")

    grid = list(itertools.product([6, 8, 10, 14], [6, 8, 10, 14, 20], [5, 10, 15]))   # sl, tgt, max minutes
    sets = (("MOMENTUM BURST", sig_burst(df)), ("MICRO PULLBACK", sig_micro_pullback(df)),
            ("RANDOM entries (baseline)", sig_random(df)))
    summary = []
    for cost in (216, 120):
        print(f"════════ cost per round-trip = ₹{cost} ════════")
        for name, sg in sets:
            best = None
            for (sl, tgt, mb) in grid:
                st = lab.stats(run(df[tr], sg[tr], sl, tgt, mb, cost))
                if st["n"] >= 25 and (best is None or st["net"] > best[0]["net"]):
                    best = (st, (sl, tgt, mb))
            if best is None:
                print(f"▶ {name}: too few trades\n"); continue
            sl, tgt, mb = best[1]
            te_st = lab.stats(run(df[te], sg[te], sl, tgt, mb, cost))
            all_st = lab.stats(run(df, sg, sl, tgt, mb, cost))
            be = (sl * DELTA * LOT + cost) / ((sl + tgt) * DELTA * LOT) * 100
            print(f"▶ {name}  best-on-train: stop {sl} / target {tgt} index pts, max {mb} min "
                  f"(needs ~{be:.0f}% wins to break even)")
            print(lab.fmt("TRAIN", best[0])); print(lab.fmt("TEST", te_st)); print(lab.fmt("ALL", all_st))
            print()
            summary.append((cost, name, all_st, te_st))
    print("═══ Summary (all days / unseen test) ═══")
    for cost, name, a, t in summary:
        print(f"  ₹{cost:<4} {name:<28} ALL n={a['n']:<4} net=₹{a['net']:>8,} PF={a['pf']:<5} | TEST n={t['n']:<4} net=₹{t['net']:>8,} PF={t['pf']}")


if __name__ == "__main__":
    main()
