"""
candle_store.py — local-first candles, so trading decisions never wait on Angel One's
rate-limited historical endpoint (getCandleData).

How it works
  * The bot already polls the index price every few seconds (cheap ltpData endpoint).
    Each price sample is fed to `add_tick()`; the store folds them into 5-minute candles.
  * Finished candles are saved to data/candles_<NAME>_5m.csv (in the persistent volume),
    so a restart reloads them instead of asking the API again.
  * The historical API is only used to (a) fetch missing history/gaps and (b) reconcile
    the locally built candles with the exchange's official ones every
    CANDLE_RECONCILE_MINUTES. API candles always overwrite local ones.
  * `closed()` returns only COMPLETED candles, from either source.

Local candles are built from ~5-second price samples, so their high/low can differ by a
few points from the exchange's official candles until the next reconciliation.
"""
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

import config

COLS = ["datetime", "open", "high", "low", "close", "volume"]
MIN_HISTORY_BARS = 120          # ~1.5 trading days of 5-min bars needed for the indicators
MARKET_OPEN = (9, 15)


class CandleStore:
    def __init__(self, name: str, minutes: int = 5, keep_days: int = 10) -> None:
        self.name, self.minutes, self.keep_days = name, minutes, keep_days
        self.path = os.path.join(config.DATA_DIR, f"candles_{name}_{minutes}m.csv")
        self._lock = threading.RLock()
        self._df = pd.DataFrame({"datetime": pd.Series(dtype="datetime64[ns]"),
                                 **{c: pd.Series(dtype="float64") for c in COLS[1:]},
                                 "src": pd.Series(dtype="object")})
        self._cur: Optional[dict] = None             # the candle being formed from ticks
        self._last_api_ok = time.monotonic()         # treat a fresh start as "just reconciled"
        self._last_api_try = -1e9                    # …but a needed gap-fill may run immediately
        self._load()

    # ── persistence ───────────────────────────────────────────

    def _load(self) -> None:
        try:
            df = pd.read_csv(self.path, parse_dates=["datetime"])
            cutoff = datetime.now() - timedelta(days=self.keep_days)
            self._df = df[df["datetime"] >= cutoff].sort_values("datetime").reset_index(drop=True)
            if len(self._df):
                logger.info(f"🕯️  [{self.name}] loaded {len(self._df)} saved candles "
                            f"({self._df['datetime'].iloc[0]:%d-%b %H:%M} → {self._df['datetime'].iloc[-1]:%d-%b %H:%M})")
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[{self.name}] candle file unreadable ({exc}) — starting empty")

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            self._df.to_csv(tmp, index=False)
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.debug(f"[{self.name}] candle save failed: {exc}")

    # ── time helpers ──────────────────────────────────────────

    def _floor(self, ts: datetime) -> datetime:
        return ts.replace(minute=ts.minute - ts.minute % self.minutes, second=0, microsecond=0)

    # ── building candles from price samples ───────────────────

    def add_tick(self, ts: datetime, price: float) -> None:
        start = self._floor(ts)
        with self._lock:
            cur = self._cur
            if cur is not None and start < cur["start"]:
                return                                   # stale sample
            if cur is None or start > cur["start"]:
                if cur is not None:
                    self._finalize(cur)
                self._cur = {"start": start, "open": price, "high": price, "low": price,
                             "close": price, "n": 1}
            else:
                cur["high"], cur["low"] = max(cur["high"], price), min(cur["low"], price)
                cur["close"] = price
                cur["n"] += 1

    def _finalize(self, cur: dict) -> None:
        if cur["n"] < 2:
            return                                       # too thin to trust
        existing = self._df["datetime"] == cur["start"]
        if existing.any() and (self._df.loc[existing, "src"] == "api").any():
            return                                       # official candle already stored
        self._upsert(pd.DataFrame([{
            "datetime": cur["start"], "open": cur["open"], "high": cur["high"],
            "low": cur["low"], "close": cur["close"], "volume": 0.0, "src": "local"}]))

    def _upsert(self, rows: pd.DataFrame) -> None:
        keep = self._df[~self._df["datetime"].isin(rows["datetime"])]
        parts = [p for p in (keep, rows) if len(p)]      # avoid concat of empty frames
        self._df = (pd.concat(parts, ignore_index=True)
                    .sort_values("datetime").reset_index(drop=True))
        self._save()

    def _close_forming(self, now: datetime) -> None:
        """Finalize the forming candle once the clock says it is over (no new tick needed)."""
        cur = self._cur
        if cur is not None and cur["start"] + timedelta(minutes=self.minutes) <= now:
            self._finalize(cur)
            self._cur = None

    # ── official candles from the API ─────────────────────────

    def merge_api(self, df_api: pd.DataFrame, last_closed_start: datetime) -> int:
        """Store official candles (closed ones only). They overwrite local candles."""
        dt = pd.to_datetime(df_api["datetime"])
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_localize(None)                 # API gives IST wall-clock with an offset
        df = df_api.copy()
        df["datetime"] = dt
        df = df[df["datetime"] <= last_closed_start][COLS].copy()
        df["src"] = "api"
        if df.empty:
            return 0
        with self._lock:
            # Measure how far our locally built candles were from the official ones.
            local = self._df[self._df["src"] == "local"][["datetime", "high", "low", "close"]]
            both = local.merge(df[["datetime", "high", "low", "close"]], on="datetime", suffixes=("_l", "_x"))
            if len(both):
                logger.info(
                    f"📏 [{self.name}] local vs official candles over {len(both)} bars: "
                    f"mean |close err| {(both.close_l - both.close_x).abs().mean():.2f} pts, "
                    f"|high err| {(both.high_l - both.high_x).abs().mean():.2f}, "
                    f"|low err| {(both.low_l - both.low_x).abs().mean():.2f}")
            self._upsert(df)
            cutoff = datetime.now() - timedelta(days=self.keep_days)
            self._df = self._df[self._df["datetime"] >= cutoff].reset_index(drop=True)
        return len(df)

    def note_api(self, ok: bool) -> None:
        self._last_api_try = time.monotonic()
        if ok:
            self._last_api_ok = self._last_api_try

    # ── reading ───────────────────────────────────────────────

    def closed(self, last_closed_start: datetime) -> pd.DataFrame:
        """Completed candles up to and including `last_closed_start` (oldest first)."""
        with self._lock:
            self._close_forming(datetime.now())
            out = self._df[self._df["datetime"] <= last_closed_start]
            return out[COLS].reset_index(drop=True)

    def has_gap(self, last_closed_start: datetime) -> bool:
        """True if history is too short or any of today's candles so far is missing."""
        with self._lock:
            if len(self._df[self._df["datetime"] <= last_closed_start]) < MIN_HISTORY_BARS:
                return True
            day_open = last_closed_start.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                                                 second=0, microsecond=0)
            if last_closed_start < day_open:
                return False
            expected = int((last_closed_start - day_open) / timedelta(minutes=self.minutes)) + 1
            today = self._df[(self._df["datetime"] >= day_open) & (self._df["datetime"] <= last_closed_start)]
            return len(today) < expected

    def api_due(self, last_closed_start: datetime) -> bool:
        """Should we ask the historical API now? (gap to fill, or periodic reconciliation)"""
        now = time.monotonic()
        if now - self._last_api_try < config.CANDLE_API_RETRY_SECS:
            return False                                  # never hammer the endpoint
        if self.has_gap(last_closed_start):
            return True
        return now - self._last_api_ok >= config.CANDLE_RECONCILE_MINUTES * 60
