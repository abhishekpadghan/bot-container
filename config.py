"""
config.py — Centralized settings loader from .env
All modules import from here — never read os.environ directly elsewhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Angel One SmartAPI Credentials ───────────────────────────
API_KEY      = os.environ["API_KEY"]
CLIENT_ID    = os.environ["CLIENT_ID"]
PASSWORD     = os.environ["PASSWORD"]
TOTP_SECRET  = os.environ["TOTP_SECRET"]

# ── Trading Mode ──────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()  # paper | live
assert TRADING_MODE in ("paper", "live"), "TRADING_MODE must be 'paper' or 'live'"

# ── Instruments — configure which to trade ───────────────────
# Single instrument:  INSTRUMENTS=NIFTY
# Multiple:           INSTRUMENTS=NIFTY,SENSEX
INSTRUMENTS_RAW = os.getenv("INSTRUMENTS", os.getenv("INSTRUMENT", "NIFTY"))
INSTRUMENTS     = [i.strip().upper() for i in INSTRUMENTS_RAW.split(",")]
INSTRUMENT      = INSTRUMENTS[0]   # primary instrument (backward compat)

EXPIRY_TYPE   = os.getenv("EXPIRY_TYPE", "weekly").lower()   # weekly | monthly
OPTION_TYPE   = os.getenv("OPTION_TYPE", "auto").lower()     # CE | PE | auto
# LOT_SIZE here is only a fallback default — each instrument uses its own
# lot size defined in market_data.INSTRUMENT_REGISTRY.
# Override per-instrument via: NIFTY_LOT_SIZE=65  SENSEX_LOT_SIZE=20
LOT_SIZE      = int(os.getenv("LOT_SIZE", "65"))   # updated: Nifty default is 65

# ── Per-instrument settings ───────────────────────────────────
# These are used by InstrumentConfig.get(name) in market_data.py
# You can override per-instrument lot size via env:
#   SENSEX_LOT_SIZE=10
#   BANKNIFTY_LOT_SIZE=30

# ── Risk Parameters ───────────────────────────────────────────
PROFIT_TARGET_POINTS = float(os.getenv("PROFIT_TARGET_POINTS", "20"))
STOP_LOSS_POINTS     = float(os.getenv("STOP_LOSS_POINTS", "10"))
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", "1500"))
DAILY_MAX_LOSS       = float(os.getenv("DAILY_MAX_LOSS", "750"))
MAX_TRADES_PER_DAY   = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
COST_PER_TRADE       = float(os.getenv("COST_PER_TRADE", "216"))        # round-trip brokerage+taxes+slippage (₹)
# Worst NET loss one stopped-out trade may cost (stop distance × lot + COST_PER_TRADE).
# Strategies cap their stop so this holds — at 750 and ₹216 costs, Nifty's stop is ≤ 8.2 premium pts.
MAX_LOSS_PER_TRADE   = float(os.getenv("MAX_LOSS_PER_TRADE", "750"))

# ── Timing ────────────────────────────────────────────────────
CANDLE_MINUTES       = 5     # strategies decide ONCE per closed candle of this size
CANDLE_SETTLE_SECS   = int(os.getenv("CANDLE_SETTLE_SECS", "6"))    # wait after a candle closes before deciding on it
# Candles are built locally from price samples; the rate-limited historical API is only used
# to fill gaps and to reconcile with the official candles this often (minutes).
CANDLE_RECONCILE_MINUTES = int(os.getenv("CANDLE_RECONCILE_MINUTES", "30"))
CANDLE_API_RETRY_SECS    = int(os.getenv("CANDLE_API_RETRY_SECS", "120"))   # min gap between API attempts
EXIT_POLL_SECS       = int(os.getenv("EXIT_POLL_SECS", "4"))        # in a trade: check stop/target this often
EXPIRY_WEEKDAY       = int(os.getenv("EXPIRY_WEEKDAY", "1"))        # Nifty weekly expiry: 0=Mon … 1=Tue
# The bot always buys the NEXT expiry's option (never the one expiring today), so the classic
# "no option buying on expiry afternoon" theta rule does not apply. It suppressed a valid Oct-13
# 14:15 TREND_DAY signal. Set to 1 only if you ever trade the expiring contract.
BLOCK_EXPIRY_AFTERNOON = os.getenv("BLOCK_EXPIRY_AFTERNOON", "0") == "1"

# ── Indicator Parameters ──────────────────────────────────────
RSI_PERIOD            = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT        = float(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD          = float(os.getenv("RSI_OVERSOLD", "30"))
EMA_FAST              = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW              = int(os.getenv("EMA_SLOW", "21"))
SUPERTREND_PERIOD     = int(os.getenv("SUPERTREND_PERIOD", "7"))
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "3.0"))
VIX_MAX_THRESHOLD     = float(os.getenv("VIX_MAX_THRESHOLD", "20.0"))

# ── Market Hours (IST) ────────────────────────────────────────
ENTRY_TIME_START = os.getenv("ENTRY_TIME_START", "09:20")   # skip opening 5 min noise
ENTRY_TIME_END   = os.getenv("ENTRY_TIME_END",   "14:30")   # no new entries after this
EXIT_ALL_TIME    = os.getenv("EXIT_ALL_TIME",     "15:15")   # force-exit before 15:30

# ── Dashboard ─────────────────────────────────────────────────
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR  = os.path.join(BASE_DIR, "logs")
DB_PATH  = os.path.join(DATA_DIR, "trades.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR,  exist_ok=True)
