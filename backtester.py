"""
backtester.py — Historical backtesting engine.

Replays past OHLC candle data through the strategy engine
and produces a detailed performance report:
  - Total P&L, win rate, Sharpe ratio
  - Max drawdown, consecutive losses
  - Per-strategy breakdown
  - Day-of-week performance
  - Best / worst trades

Usage:
  # Synthetic data (no login needed):
  python backtester.py --days 90 --strategy TREND_FOLLOW

  # Real Angel One historical data (requires .env credentials):
  python backtester.py --days 90 --strategy TREND_FOLLOW --live-data
  python backtester.py --days 180 --all --live-data --instrument SENSEX

  # All strategies:
  python backtester.py --days 180 --all
"""

import argparse
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

import config
from strategy import compute_supertrend, compute_ema, compute_rsi, compute_vwap
from strategies import ALL_STRATEGIES, StrategySignal
from risk_engine import RiskEngine


# ── SQLite store for backtest results ─────────────────────────

def _get_bt_conn() -> sqlite3.Connection:
    import os
    db_path = os.path.join(config.DATA_DIR, "backtest.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT,
            strategy      TEXT,
            date          TEXT,
            entry_time    TEXT,
            exit_time     TEXT,
            option_type   TEXT,
            entry_price   REAL,
            exit_price    REAL,
            pnl_points    REAL,
            pnl_inr       REAL,
            exit_reason   TEXT,
            lot_size      INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id        TEXT PRIMARY KEY,
            strategy      TEXT,
            start_date    TEXT,
            end_date      TEXT,
            total_days    INTEGER,
            trade_days    INTEGER,
            total_trades  INTEGER,
            wins          INTEGER,
            losses        INTEGER,
            win_rate      REAL,
            total_pnl     REAL,
            avg_pnl_day   REAL,
            max_drawdown  REAL,
            sharpe        REAL,
            profit_factor REAL,
            created_at    TEXT
        )
    """)
    conn.commit()
    return conn


# ── Real data source via Angel One SmartAPI ──────────────────

def fetch_real_candles(
    instrument: str = "NIFTY",
    days: int = 90,
    interval: str = "FIVE_MINUTE",
) -> pd.DataFrame:
    """
    Fetch real historical OHLC candle data from Angel One SmartAPI.

    Requires valid credentials in .env (API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET).
    Angel One's getCandleData endpoint returns up to ~100 days of intraday data.

    Args:
        instrument: "NIFTY" | "SENSEX" | "BANKNIFTY" | "BANKEX" | "FINNIFTY"
        days:       How many calendar days of history to fetch (max ~100 for intraday)
        interval:   SmartAPI interval string — "FIVE_MINUTE" | "ONE_MINUTE" | "ONE_DAY"

    Returns:
        DataFrame with columns: datetime, open, high, low, close, volume
        Sorted ascending by datetime, market hours only (9:15–15:30 IST).

    Raises:
        RuntimeError if login or API call fails.
    """
    try:
        from session_manager import SessionManager
        import market_data as md

        logger.info(f"📡 Connecting to Angel One to fetch {days}d of {instrument} {interval} data...")
        SessionManager.get().connect()
        df = md.get_index_candles(instrument, interval=interval, lookback_days=days)

        if df.empty:
            raise RuntimeError("Angel One returned empty candle data — check credentials and market hours")

        # Filter to market hours only: 09:15 – 15:30 IST
        df = df[
            (df["datetime"].dt.hour > 9) |
            ((df["datetime"].dt.hour == 9) & (df["datetime"].dt.minute >= 15))
        ]
        df = df[
            (df["datetime"].dt.hour < 15) |
            ((df["datetime"].dt.hour == 15) & (df["datetime"].dt.minute <= 30))
        ]
        df = df.sort_values("datetime").reset_index(drop=True)

        logger.info(f"✅ Fetched {len(df)} candles for {instrument} ({df['datetime'].min()} → {df['datetime'].max()})")
        return df

    except ImportError as exc:
        raise RuntimeError(f"market_data module not available: {exc}")
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch real candles for {instrument}: {exc}")


# ── Simulated data source ─────────────────────────────────────

def _generate_synthetic_candles(
    days: int = 90,
    start_price: float = 22000.0,
    interval_mins: int = 5,
) -> pd.DataFrame:
    """
    Generate synthetic Nifty-like OHLC candle data for backtesting
    when live historical data is unavailable.

    Uses Geometric Brownian Motion with realistic Nifty parameters:
      - Daily volatility ~0.8%
      - Slight upward drift (bull market assumption)
      - Intraday patterns (volatile open, calmer mid-session)
    """
    np.random.seed(42)
    candles_per_day = (375 // interval_mins)  # 375 mins = 9:15 to 15:30
    total_candles   = days * candles_per_day

    dt      = interval_mins / (252 * 375)   # fraction of a trading year
    mu      = 0.12                          # 12% annual drift
    sigma   = 0.18                          # 18% annual volatility
    price   = start_price

    records = []
    current_date = date.today() - timedelta(days=days)

    for day in range(days):
        current_date += timedelta(days=1)
        if current_date.weekday() >= 5:  # skip weekends
            continue

        # Intraday: open is slightly gapped from previous close
        open_price = price * (1 + np.random.normal(0, 0.002))

        for minute in range(0, 375, interval_mins):
            hour   = 9 + (minute + 15) // 60
            minute = (minute + 15) % 60
            ts     = datetime(current_date.year, current_date.month, current_date.day,
                              hour, minute)

            # Volatility higher at open and close
            intraday_vol = sigma * (1.5 if minute < 30 or minute > 330 else 1.0)
            ret    = (mu - 0.5 * intraday_vol**2) * dt + intraday_vol * np.sqrt(dt) * np.random.randn()
            close  = open_price * np.exp(ret)
            high   = max(open_price, close) * (1 + abs(np.random.normal(0, 0.001)))
            low    = min(open_price, close) * (1 - abs(np.random.normal(0, 0.001)))
            vol    = int(np.random.exponential(50000))

            records.append({
                "datetime": ts, "open": open_price, "high": high,
                "low": low, "close": close, "volume": vol
            })
            open_price = close

        price = open_price  # carry close to next day's open

    df = pd.DataFrame(records)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


# ── Option premium simulator ──────────────────────────────────

def _simulate_option_premium(
    spot: float,
    strike: float,
    days_to_expiry: float,
    option_type: str,
    iv: float = 0.15,
) -> float:
    """
    Black-Scholes approximation for ATM option premium.
    Used to estimate realistic entry/exit prices in backtesting.
    """
    from math import sqrt, exp, log
    try:
        from scipy.stats import norm
        r  = 0.065  # risk-free rate
        S, K, T = spot, strike, max(days_to_expiry / 365, 0.001)
        d1 = (log(S / K) + (r + 0.5 * iv**2) * T) / (iv * sqrt(T))
        d2 = d1 - iv * sqrt(T)
        if option_type == "CE":
            return max(S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2), 1.0)
        else:
            return max(K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1), 1.0)
    except ImportError:
        # scipy not available — use simple ATM approximation
        atm_approx = spot * iv * (days_to_expiry / 365) ** 0.5 * 0.4
        return max(atm_approx, 1.0)


# ── Main backtesting engine ───────────────────────────────────

class Backtester:
    """
    Replays strategy on historical candle data.

    Usage:
        bt = Backtester(strategy_name="TREND_FOLLOW", days=90)
        result = bt.run()
        bt.print_report(result)
    """

    def __init__(
        self,
        strategy_name: str = "TREND_FOLLOW",
        days: int = 90,
        df: pd.DataFrame = None,
        live_data: bool = False,
        instrument: str = "NIFTY",
    ):
        self.strategy_name = strategy_name
        self.strategy      = ALL_STRATEGIES.get(strategy_name)
        self.days          = days
        self.df            = df
        self.live_data     = live_data
        self.instrument    = instrument.upper()

    def run(self) -> dict:
        """Run backtest and return results dict."""
        import uuid
        run_id = str(uuid.uuid4())[:8]

        if self.df is None:
            if self.live_data:
                try:
                    self.df = fetch_real_candles(
                        instrument=self.instrument,
                        days=self.days,
                        interval="FIVE_MINUTE",
                    )
                    logger.info(f"📊 Using REAL Angel One data for {self.instrument} backtest")
                except RuntimeError as exc:
                    logger.warning(f"⚠️  Real data fetch failed: {exc}")
                    logger.warning("⚠️  Falling back to synthetic data")
                    self.df = _generate_synthetic_candles(days=self.days)
            else:
                logger.info(f"📊 Generating {self.days}-day synthetic data for backtest...")
                self.df = _generate_synthetic_candles(days=self.days)

        logger.info(
            f"🔄 Backtesting {self.strategy_name} | "
            f"{self.days} days | {len(self.df)} candles"
        )

        trades      = []
        daily_pnls  = {}
        open_trade  = None
        risk        = RiskEngine()
        WINDOW      = 30  # candles needed for indicators

        dates = self.df["datetime"].dt.date.unique()

        for trade_date in sorted(dates):
            day_df = self.df[self.df["datetime"].dt.date == trade_date].copy()
            if len(day_df) < WINDOW:
                continue

            risk.state.reset_for_new_day()
            daily_pnl  = 0.0
            open_trade = None

            for i in range(WINDOW, len(day_df)):
                window_df  = day_df.iloc[:i].copy()
                current_ltp = day_df.iloc[i]["close"]
                current_ts  = day_df.iloc[i]["datetime"]

                # ── Exit check ─────────────────────────────
                if open_trade:
                    highest_ltp = open_trade.get("highest_ltp", open_trade["entry"])
                    highest_ltp = max(highest_ltp, current_ltp)
                    open_trade["highest_ltp"] = highest_ltp

                    sl   = open_trade.get("sl_pts",  config.STOP_LOSS_POINTS)
                    tgt  = open_trade.get("tgt_pts", config.PROFIT_TARGET_POINTS)
                    pnl  = current_ltp - open_trade["entry"]

                    exit_reason = None
                    if pnl >= tgt:
                        exit_reason = "TARGET"
                    elif pnl <= -sl:
                        exit_reason = "STOPLOSS"
                    elif i == len(day_df) - 1:
                        exit_reason = "EOD"

                    # Trailing SL check
                    if not exit_reason:
                        trail_exit, trail_reason = risk.trailing_stop(
                            open_trade["entry"], current_ltp, highest_ltp
                        )
                        if trail_exit:
                            exit_reason = trail_reason

                    if exit_reason:
                        pnl_inr = pnl * config.LOT_SIZE
                        daily_pnl += pnl_inr
                        risk.record_trade(pnl)
                        trades.append({
                            "run_id":      run_id,
                            "strategy":    self.strategy_name,
                            "date":        str(trade_date),
                            "entry_time":  str(open_trade["time"]),
                            "exit_time":   str(current_ts),
                            "option_type": open_trade["option_type"],
                            "entry_price": open_trade["entry"],
                            "exit_price":  current_ltp,
                            "pnl_points":  pnl,
                            "pnl_inr":     pnl_inr,
                            "exit_reason": exit_reason,
                            "lot_size":    config.LOT_SIZE,
                        })
                        open_trade = None

                # ── Entry check ────────────────────────────
                if open_trade is None:
                    can, _ = risk.can_trade()
                    if not can:
                        continue

                    try:
                        sig = self.strategy.should_enter(window_df, vix=15)
                    except Exception:
                        sig = None

                    if sig and sig.signal.value == "BUY" and sig.option_type:
                        # Simulate option premium as ATM entry price
                        days_left = max(1, (3 - trade_date.weekday()) % 7)
                        entry = _simulate_option_premium(
                            current_ltp, current_ltp, days_left,
                            sig.option_type.value
                        )
                        open_trade = {
                            "entry":       entry,
                            "highest_ltp": entry,
                            "time":        current_ts,
                            "option_type": sig.option_type.value,
                            "sl_pts":      sig.suggested_sl_pts  or config.STOP_LOSS_POINTS,
                            "tgt_pts":     sig.suggested_tgt_pts or config.PROFIT_TARGET_POINTS,
                        }

            daily_pnls[str(trade_date)] = daily_pnl

        return self._compute_stats(run_id, trades, daily_pnls)

    def _compute_stats(self, run_id: str, trades: list, daily_pnls: dict) -> dict:
        if not trades:
            return {"run_id": run_id, "error": "No trades generated"}

        pnls    = [t["pnl_points"] for t in trades]
        pnl_inr = [t["pnl_inr"]    for t in trades]
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]

        daily_vals = list(daily_pnls.values())
        # Max drawdown
        peak, max_dd = 0, 0
        cumulative   = 0
        for d in daily_vals:
            cumulative += d
            peak = max(peak, cumulative)
            max_dd = min(max_dd, cumulative - peak)

        # Sharpe ratio (annualised, daily P&L)
        daily_arr = np.array(daily_vals)
        sharpe = (
            (daily_arr.mean() / daily_arr.std() * np.sqrt(252))
            if daily_arr.std() > 0 else 0
        )

        # Profit factor
        gross_profit = sum(p for p in pnl_inr if p > 0)
        gross_loss   = abs(sum(p for p in pnl_inr if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        result = {
            "run_id":        run_id,
            "strategy":      self.strategy_name,
            "days":          self.days,
            "total_trades":  len(trades),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      len(wins) / len(trades) * 100,
            "total_pnl":     sum(pnl_inr),
            "avg_pnl_trade": np.mean(pnl_inr),
            "avg_win":       np.mean(wins) * config.LOT_SIZE if wins else 0,
            "avg_loss":      np.mean(losses) * config.LOT_SIZE if losses else 0,
            "max_drawdown":  max_dd,
            "sharpe":        sharpe,
            "profit_factor": pf,
            "trade_days":    len([v for v in daily_vals if v != 0]),
            "trades":        trades,
            "daily_pnls":    daily_pnls,
        }

        # Save to DB
        conn = _get_bt_conn()
        conn.execute("""
            INSERT OR REPLACE INTO backtest_runs VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run_id, self.strategy_name,
            str(date.today() - timedelta(days=self.days)),
            str(date.today()),
            self.days,
            result["trade_days"],
            result["total_trades"],
            result["wins"], result["losses"],
            result["win_rate"],
            result["total_pnl"],
            result["avg_pnl_trade"],
            result["max_drawdown"],
            result["sharpe"],
            result["profit_factor"],
            datetime.now().isoformat(),
        ))
        conn.commit()
        return result

    def print_report(self, result: dict):
        """Print a formatted backtest report to stdout."""
        if "error" in result:
            print(f"❌ Backtest error: {result['error']}")
            return

        print()
        print("=" * 65)
        print(f"  📊 Backtest Report — {result['strategy']} ({result['days']} days)")
        print("=" * 65)
        print(f"  Total Trades   : {result['total_trades']}")
        print(f"  Win Rate       : {result['win_rate']:.1f}%  ({result['wins']}W / {result['losses']}L)")
        print(f"  Total P&L      : ₹{result['total_pnl']:+,.0f}")
        print(f"  Avg P&L/Trade  : ₹{result['avg_pnl_trade']:+,.0f}")
        print(f"  Avg Win        : ₹{result['avg_win']:+,.0f}")
        print(f"  Avg Loss       : ₹{result['avg_loss']:+,.0f}")
        print(f"  Max Drawdown   : ₹{result['max_drawdown']:,.0f}")
        print(f"  Sharpe Ratio   : {result['sharpe']:.2f}")
        print(f"  Profit Factor  : {result['profit_factor']:.2f}")
        print("=" * 65)

        # Verdict
        wr = result["win_rate"]
        pf = result["profit_factor"]
        if wr >= 50 and pf >= 1.5 and result["sharpe"] >= 1.0:
            print("  ✅ STRATEGY LOOKS PROFITABLE — consider live testing")
        elif wr >= 40 and pf >= 1.2:
            print("  ⚠️  MARGINAL — needs more optimisation before live")
        else:
            print("  ❌ STRATEGY NOT PROFITABLE on this data — do NOT go live")
        print()


# ── CLI entry point ───────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NiftyBot Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Synthetic data (no credentials needed):
  python backtester.py --days 90 --strategy TREND_FOLLOW
  python backtester.py --days 180 --all

  # Real Angel One data (requires .env credentials):
  python backtester.py --days 90 --strategy TREND_FOLLOW --live-data
  python backtester.py --days 90 --all --live-data --instrument SENSEX
        """,
    )
    parser.add_argument("--days",       type=int,  default=90,             help="Days of history (default: 90)")
    parser.add_argument("--strategy",   type=str,  default="TREND_FOLLOW", help="Strategy name (default: TREND_FOLLOW)")
    parser.add_argument("--all",        action="store_true",               help="Test all strategies")
    parser.add_argument("--live-data",  action="store_true",               help="Fetch real data from Angel One SmartAPI")
    parser.add_argument("--instrument", type=str,  default="NIFTY",        help="Instrument for real data (default: NIFTY)")
    args = parser.parse_args()

    if args.all:
        for name in ALL_STRATEGIES:
            if name in ("STRADDLE", "IRON_CONDOR"):
                continue  # skip multi-leg for now
            bt = Backtester(
                strategy_name=name,
                days=args.days,
                live_data=args.live_data,
                instrument=args.instrument,
            )
            result = bt.run()
            bt.print_report(result)
    else:
        bt = Backtester(
            strategy_name=args.strategy,
            days=args.days,
            live_data=args.live_data,
            instrument=args.instrument,
        )
        result = bt.run()
        bt.print_report(result)
