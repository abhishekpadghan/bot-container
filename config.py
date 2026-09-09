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
