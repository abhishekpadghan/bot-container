"""
research/strategy_lab.py — honest backtest of intraday NIFTY option-buying ideas
on REAL 5-minute index candles (research/data/nifty_FIVE_MINUTE.csv).

What it models (and what it does not):
  * Signals use CLOSED bars only; entry is at the NEXT bar's open (no look-ahead).
  * A bought ATM weekly option is modelled from the index move:
        premium(t) = P0 + DELTA * (index move in our favour) - theta * bars_held
    with P0 = 0.4 * spot * sigma * sqrt(days_to_expiry / 365).
    This ignores gamma/vega/IV changes, so treat results as indicative, not exact.
  * Stop-loss is checked BEFORE target inside a bar (pessimistic ordering).
  * Round-trip cost of Rs 216 per trade (brokerage + taxes + slippage) is deducted.
  * Same day rules as the bot: max 3 trades/day, stop for the day at -750 / +1500 net.
  * Parameters are picked on the first ~2/3 of days (TRAIN) and judged on the last
    ~1/3 (TEST), which the picker never sees.

Run:  podman-compose --profile tools run --rm --no-deps cli research/strategy_lab.py
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LOT, COST, DELTA, SIGMA = 65, 216.0, 0.5, 0.15
MAX_TRADES, DAY_STOP, DAY_TARGET = 3, -750.0, 1500.0
BARS_PER_DAY = 75


# ── Data + indicators ─────────────────────────────────────────

def load_5m() -> pd.DataFrame:
    df = pd.read_csv(os.path.join(HERE, "data", "nifty_FIVE_MINUTE.csv"))
    df["datetime"] = pd.to_datetime(df["datetime"].str[:19])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.date
    df["tod"] = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute   # bar OPEN minute-of-day
    df["bar"] = df.groupby("date").cumcount()
    return df


def supertrend(df: pd.DataFrame, period=7, mult=3.0) -> pd.Series:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(span=period, adjust=False).mean().values
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    d = np.zeros(len(df), dtype=int)
    d[0] = 1
    for i in range(1, len(df)):
        if c[i] > ub[i - 1]:
            d[i] = 1
        elif c[i] < lb[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return pd.Series(d, index=df.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    tp = (h + l + c) / 3
    # Index candles carry ZERO volume, so a true VWAP does not exist. Use the
    # session average of typical price as a stand-in.
    df["vwap"] = tp.groupby(df["date"]).transform(lambda s: s.expanding().mean())
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(span=14, adjust=False).mean()
    ls = (-d).clip(lower=0).ewm(span=14, adjust=False).mean()
    df["rsi"] = (100 - 100 / (1 + g / ls.replace(0, np.nan))).fillna(50)
    df["st"] = supertrend(df)
    df["st_flip"] = df["st"].diff().fillna(0)

    # 15-minute trend, aligned so a 5m bar only sees 15m bars that have CLOSED.
    m15 = (df.set_index("datetime")
             .resample("15min")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
             .dropna())
    m15["e20"] = m15["close"].ewm(span=20, adjust=False).mean()
    m15["e50"] = m15["close"].ewm(span=50, adjust=False).mean()
    m15["avail"] = m15.index + pd.Timedelta(minutes=15)
    m15 = m15.reset_index(drop=True)[["avail", "close", "e20", "e50"]].rename(
        columns={"close": "c15"})
    left = df[["datetime"]].copy()
    left["t_close"] = left["datetime"] + pd.Timedelta(minutes=5)
    mm = pd.merge_asof(left.sort_values("t_close"), m15.sort_values("avail"),
                       left_on="t_close", right_on="avail", direction="backward")
    df["c15"], df["e20_15"], df["e50_15"] = mm["c15"].values, mm["e20"].values, mm["e50"].values

    # Opening range = first three 5m bars (09:15-09:30); usable from the 09:30 bar.
    first3 = df[df["bar"] < 3].groupby("date").agg(or_hi=("high", "max"), or_lo=("low", "min"))
    df = df.join(first3, on="date")
    day = df.groupby("date").agg(dh=("high", "max"), dl=("low", "min"), dc=("close", "last"))
    prev = day.shift(1).rename(columns={"dh": "pdh", "dl": "pdl", "dc": "pdc"})
    df = df.join(prev[["pdh", "pdl", "pdc"]], on="date")
    first_open = df[df["bar"] == 0].set_index("date")["open"].rename("day_open")
    df = df.join(first_open, on="date")
    return df


# ── Signal generators: +1 = buy CE, -1 = buy PE, 0 = none (evaluated at bar close) ──

def sig_exist_breakout(df, lookback=10, min_range=30):
    """The bot's current BREAKOUT rule (close beyond the prior 10-bar range)."""
    hi = df["high"].shift(1).rolling(lookback).max()
    lo = df["low"].shift(1).rolling(lookback).min()
    ok = (hi - lo) >= min_range
    s = np.where(ok & (df["close"] > hi), 1, np.where(ok & (df["close"] < lo), -1, 0))
    return pd.Series(s, index=df.index)


def sig_exist_trend(df):
    """The bot's TREND_FOLLOW scoring (3 of 4), with VWAP proxy and BOTH directions."""
    bull = ((df["st"] == 1).astype(int) + (df["ema9"] > df["ema21"]).astype(int)
            + df["rsi"].between(45, 65).astype(int) + (df["close"] > df["vwap"]).astype(int))
    bear = ((df["st"] == -1).astype(int) + (df["ema9"] < df["ema21"]).astype(int)
            + df["rsi"].between(35, 55).astype(int) + (df["close"] < df["vwap"]).astype(int))
    s = np.where(bull >= 3, 1, np.where(bear >= 3, -1, 0))
    return pd.Series(s, index=df.index)


def sig_orb(df, buf_frac=0.1, min_or=25, max_or=140, need_trend=True, nbars=3):
    """Opening-range breakout: first close beyond the opening range, once per day."""
    g = df[df["bar"] < nbars].groupby("date").agg(h=("high", "max"), l=("low", "min"))
    or_hi, or_lo = df["date"].map(g["h"]), df["date"].map(g["l"])
    rng = or_hi - or_lo
    buf = np.maximum(rng * buf_frac, 3)
    up = (df["close"] > or_hi + buf) & (df["bar"] >= nbars)
    dn = (df["close"] < or_lo - buf) & (df["bar"] >= nbars)
    ok = rng.between(min_or, max_or)
    if need_trend:
        up &= (df["close"] > df["vwap"]) & (df["e20_15"] >= df["e50_15"])
        dn &= (df["close"] < df["vwap"]) & (df["e20_15"] <= df["e50_15"])
    s = pd.Series(np.where(ok & up, 1, np.where(ok & dn, -1, 0)), index=df.index)
    # only the FIRST qualifying signal of each day
    first = s.ne(0).groupby(df["date"]).cumsum().eq(1) & s.ne(0)
    return s.where(first, 0)


def sig_pullback(df, rsi_lo=40, rsi_hi=62):
    """15m trend + 5m pullback to EMA21 that closes back above EMA9 / prior high."""
    up_tr = (df["e20_15"] > df["e50_15"]) & (df["c15"] > df["e20_15"])
    dn_tr = (df["e20_15"] < df["e50_15"]) & (df["c15"] < df["e20_15"])
    touched_dn = (df["low"].rolling(4).min() <= df["ema21"]) & (df["close"] > df["ema9"]) \
        & (df["close"] > df["high"].shift(1)) & df["rsi"].between(rsi_lo, rsi_hi)
    touched_up = (df["high"].rolling(4).max() >= df["ema21"]) & (df["close"] < df["ema9"]) \
        & (df["close"] < df["low"].shift(1)) & df["rsi"].between(100 - rsi_hi, 100 - rsi_lo)
    s = np.where(up_tr & touched_dn, 1, np.where(dn_tr & touched_up, -1, 0))
    return pd.Series(s, index=df.index)


def sig_st_flip(df, need_trend=True):
    """Supertrend flip, filtered by 15m trend direction."""
    s = df["st_flip"].apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    if need_trend:
        s = s.where(~((s == 1) & (df["e20_15"] < df["e50_15"])), 0)
        s = s.where(~((s == -1) & (df["e20_15"] > df["e50_15"])), 0)
    return s


def sig_vwap_fade(df, k=1.8):
    """Fade stretched moves away from the session mean, only in non-trending 15m regime."""
    dev = (df["close"] - df["vwap"]) / df["atr"]
    flat = ((df["e20_15"] - df["e50_15"]).abs() / df["atr"]) < 1.5
    s = np.where(flat & (dev <= -k) & (df["rsi"] < 35), 1,
                 np.where(flat & (dev >= k) & (df["rsi"] > 65), -1, 0))
    return pd.Series(s, index=df.index)


def sig_trend_day(df, min_move=0.004, first_bar=12):
    """Continuation on a clear trend day: price well away from the open, aligned with
    session mean and EMAs, and printing a fresh session high/low. First signal per day."""
    mv = (df["close"] - df["day_open"]) / df["day_open"]
    sess_hi = df.groupby("date")["high"].transform(lambda s: s.shift(1).expanding().max())
    sess_lo = df.groupby("date")["low"].transform(lambda s: s.shift(1).expanding().min())
    up = (mv >= min_move) & (df["close"] > df["vwap"]) & (df["ema9"] > df["ema21"]) & (df["close"] > sess_hi)
    dn = (mv <= -min_move) & (df["close"] < df["vwap"]) & (df["ema9"] < df["ema21"]) & (df["close"] < sess_lo)
    s = pd.Series(np.where(up & (df["bar"] >= first_bar), 1, np.where(dn & (df["bar"] >= first_bar), -1, 0)), index=df.index)
    first = s.ne(0).groupby(df["date"]).cumsum().eq(1) & s.ne(0)
    return s.where(first, 0)


def sig_pdh_break(df):
    """Break of the previous day's high/low with 15m trend alignment. First per day."""
    up = (df["close"] > df["pdh"]) & (df["close"].shift(1) <= df["pdh"]) & (df["e20_15"] > df["e50_15"])
    dn = (df["close"] < df["pdl"]) & (df["close"].shift(1) >= df["pdl"]) & (df["e20_15"] < df["e50_15"])
    s = pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=df.index)
    first = s.ne(0).groupby(df["date"]).cumsum().eq(1) & s.ne(0)
    return s.where(first, 0)


# ── Trade simulation ──────────────────────────────────────────

def days_to_expiry(d) -> int:
    n = (1 - d.weekday()) % 7           # Tuesday weekly expiry
    return n if n else 7                # the bot skips an expiring contract on expiry day


def simulate(day, i_entry, dirn, sl_idx, tgt_idx, trail_at, trail_dist, t_days, last_bar):
    o, h, l, c = day["open"].values, day["high"].values, day["low"].values, day["close"].values
    e = o[i_entry]
    t_eff = max(t_days - day["bar"].values[i_entry] / BARS_PER_DAY, 0.3)
    p0 = 0.4 * e * SIGMA * np.sqrt(t_eff / 365)
    theta_bar = p0 / (2 * t_eff * BARS_PER_DAY)
    stop, peak, reason = -sl_idx, 0.0, "EOD"
    exit_fav, k_exit = None, last_bar
    for k in range(i_entry, last_bar + 1):
        fav_hi = (h[k] - e) if dirn > 0 else (e - l[k])
        fav_lo = (l[k] - e) if dirn > 0 else (e - h[k])
        if fav_lo <= stop:
            exit_fav, k_exit = stop, k
            reason = "SL" if stop <= -sl_idx + 1e-9 else "TRAIL"
            break
        if tgt_idx and fav_hi >= tgt_idx:
            exit_fav, k_exit, reason = tgt_idx, k, "TGT"
            break
        peak = max(peak, fav_hi)
        if trail_at and peak >= trail_at:
            stop = max(stop, peak - trail_dist)
    if exit_fav is None:
        exit_fav = (c[last_bar] - e) * dirn
    held = k_exit - i_entry + 1
    p_exit = max(p0 + DELTA * exit_fav - theta_bar * held, 1.0)
    return (p_exit - p0) * LOT - COST, k_exit, reason, p0


def run(df, sig, sl_atr, rr, trail_r=None, trail_d=0.5, t_start=9 * 60 + 30, t_end=14 * 60 + 15,
        fixed_sl=None, fixed_tgt=None, trail_at_idx=None, trail_dist_idx=None,
        sl_floor=8.0, sl_cap=None, sl_arr=None):
    """Walk the days, take signals, simulate. Returns a DataFrame of trades."""
    trades = []
    for d, day in df.groupby("date", sort=True):
        day = day.reset_index(drop=True)
        s = sig.loc[df["date"] == d].values
        sv = sl_arr.loc[df["date"] == d].values if sl_arr is not None else None
        last_bar = int((day["tod"] <= 15 * 60 + 10).values.nonzero()[0].max())   # bar closing 15:15
        t_days, pnl_day, n, i = days_to_expiry(d), 0.0, 0, 0
        while i < len(day) - 1 and n < MAX_TRADES and DAY_STOP < pnl_day < DAY_TARGET:
            if s[i] != 0 and t_start <= day["tod"].iloc[i] <= t_end and i + 1 <= last_bar - 1:
                atr = day["atr"].iloc[i]
                sl = fixed_sl or min(max((sv[i] if sv is not None else sl_atr * atr), sl_floor), sl_cap or 1e12)
                tgt = fixed_tgt if fixed_sl else (sl * rr if rr else None)
                ta = trail_at_idx if fixed_sl else (sl * trail_r if trail_r else None)
                td = trail_dist_idx if fixed_sl else (sl * trail_d if trail_r else None)
                pnl, k_exit, reason, p0 = simulate(day, i + 1, int(s[i]), sl, tgt, ta, td, t_days, last_bar)
                trades.append({"date": d, "dir": int(s[i]), "pnl": pnl, "reason": reason,
                               "sl_idx": sl, "p0": p0, "bars": k_exit - i})
                pnl_day += pnl
                n += 1
                i = k_exit + 1
            else:
                i += 1
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0, "win%": 0, "net": 0, "avg": 0, "pf": 0, "maxdd": 0}
    w, lo = t.loc[t.pnl > 0, "pnl"].sum(), -t.loc[t.pnl < 0, "pnl"].sum()
    eq = t.sort_values("date")["pnl"].cumsum()
    return {"n": len(t), "win%": round(100 * (t.pnl > 0).mean(), 1), "net": round(t.pnl.sum()),
            "avg": round(t.pnl.mean()), "pf": round(w / lo, 2) if lo else 9.99,
            "maxdd": round((eq - eq.cummax()).min())}


def fmt(tag, s):
    return (f"  {tag:<6} n={s['n']:>3}  win%={s['win%']:>5}  net=₹{s['net']:>8,}  "
            f"avg=₹{s['avg']:>5}  PF={s['pf']:<5} maxDD=₹{s['maxdd']:,}")


def main():
    df = add_indicators(load_5m())
    dates = sorted(df["date"].unique())
    cut = dates[int(len(dates) * 2 / 3)]
    tr_mask, te_mask = df["date"] < cut, df["date"] >= cut
    print(f"{len(dates)} trading days {dates[0]} -> {dates[-1]} | TRAIN < {cut} <= TEST "
          f"({len(set(df[tr_mask].date))}/{len(set(df[te_mask].date))} days)")
    print(f"avg 5m ATR={df['atr'].mean():.1f} pts | median OR range="
          f"{(df['or_hi']-df['or_lo']).median():.0f} pts | model: delta={DELTA}, cost=₹{COST:.0f}")

    # How often does the bot's regime filter call the market 'trending'?
    slope = df["ema9"] - df["ema9"].shift(4)
    trending = (slope.abs() > df["close"].shift(4) * 0.003)
    print(f"\nBot regime check: 'TRENDING' on only {100*trending.mean():.1f}% of bars "
          f"(needs EMA9 to move >0.3% in 20 min) → TREND_FOLLOW is almost never allowed to run.")

    results = []

    def evaluate(name, sig, grid, **kw):
        best = None
        for params in grid:
            tt = run(df[tr_mask], sig[tr_mask], **params, **kw)
            st = stats(tt)
            if st["n"] >= 15 and (best is None or st["net"] > best[0]["net"]):
                best = (st, params)
        if best is None:
            print(f"\n{name}: too few trades to evaluate")
            return
        params = best[1]
        te = stats(run(df[te_mask], sig[te_mask], **params, **kw))
        print(f"\n{name}   best-on-train params: {params}")
        print(fmt("TRAIN", best[0]))
        print(fmt("TEST", te))
        results.append((name, params, best[0], te))

    # Exit grid: stop in ATRs, optional fixed target (R multiples), optional trail
    # (activate after `tr` R, trail `td` R behind the peak). Wider/trail-only exits let
    # winners run so the Rs 216 cost matters less.
    grid = [dict(sl_atr=a, rr=r, trail_r=tr, trail_d=td)
            for a, r, (tr, td) in itertools.product(
                [1.0, 1.5, 2.0, 3.0], [None, 2.0, 3.0, 4.0],
                [(None, 0.5), (1.0, 0.5), (1.5, 1.0), (2.0, 1.0)])
            if not (r is None and tr is None)]

    # (1) the bot as it stands today: premium SL 10 / target 20 => index 20 / 40, trail after target
    for name, sg in (("CURRENT breakout (bot rule, fixed 20/40 idx pts)", sig_exist_breakout(df)),
                     ("CURRENT trend-follow (VWAP proxy, both sides)", sig_exist_trend(df))):
        a = stats(run(df[tr_mask], sg[tr_mask], 0, 0, fixed_sl=20, fixed_tgt=None,
                      trail_at_idx=40, trail_dist_idx=10))
        b = stats(run(df[te_mask], sg[te_mask], 0, 0, fixed_sl=20, fixed_tgt=None,
                      trail_at_idx=40, trail_dist_idx=10))
        print(f"\n{name}")
        print(fmt("TRAIN", a))
        print(fmt("TEST", b))

    # (2) candidates with ATR-scaled risk
    evaluate("ORB 15m range (trend-filtered)", sig_orb(df), grid)
    evaluate("ORB 30m range (trend-filtered)", sig_orb(df, nbars=6, min_or=40, max_or=200), grid)
    evaluate("ORB 30m range (no filter)", sig_orb(df, nbars=6, min_or=40, max_or=200, need_trend=False), grid)
    evaluate("TREND-DAY continuation (0.4% move)", sig_trend_day(df), grid)
    evaluate("TREND-DAY continuation (0.6% move)", sig_trend_day(df, 0.006), grid)
    evaluate("PREV-DAY high/low break", sig_pdh_break(df), grid)
    evaluate("PULLBACK (15m trend)", sig_pullback(df), grid)
    evaluate("BREAKOUT (bot rule, ATR risk)", sig_exist_breakout(df), grid)
    evaluate("VWAP FADE (range regime)", sig_vwap_fade(df), grid)

    print("\n=== Summary: TEST (unseen) net P&L per candidate ===")
    for name, params, trn, tst in sorted(results, key=lambda r: -r[3]["net"]):
        print(f"  {name:<46} TEST net=₹{tst['net']:>7,}  PF={tst['pf']:<5} n={tst['n']:<3} "
              f"| train net=₹{trn['net']:,}")


if __name__ == "__main__":
    sys.exit(main())
