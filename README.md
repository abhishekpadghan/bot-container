# 📈 NiftyBot v2 — Professional Nifty F&O Algo Trading Bot

> Fully automated options trading bot for **Nifty 50** via **Angel One SmartAPI**.  
> Paper trading by default — switch to live only after proven profitability.

---

## 🎯 Goal

| Parameter | Value |
|---|---|
| Daily Profit Target | ₹1,500 (net after costs) |
| Max Loss per Trade | ₹750 (hard stop-loss) |
| Trailing Stop Activates | After +12 points profit |
| Risk : Reward | 1 : 2 (10 pts SL → 20 pts target) |
| Instruments | NIFTY 50, SENSEX (configurable — add more in `.env`) |
| Transaction Costs | ₹216/trade deducted (brokerage + STT + slippage) |

---

## 🏗️ Architecture — All Files

```
nifty_bot/
│
├── 🐳 Container
│   ├── Dockerfile            ← Python 3.11-slim, gosu, IST timezone
│   ├── podman-compose.yml    ← 3 services: bot, dashboard, cli
│   ├── entrypoint.sh         ← Fixes volume permissions, drops to botuser
│   ├── .env.example          ← All config variables with defaults
│   ├── .dockerignore         ← Keeps secrets out of image
│   └── run.sh                ← Single-command management wrapper
│
├── ⚙️ Core
│   ├── config.py             ← Loads all settings from .env
│   ├── auth.py               ← Angel One TOTP login (legacy, kept for compat)
│   └── session_manager.py    ← Production session: auto-reconnect, heartbeat,
│                                exponential-backoff retry
│
├── 📡 Market Data
│   └── market_data.py        ← Live LTP, OHLC candles, VIX, ATM strike,
│                                symbol token, OI liquidity check
│
├── 🧠 Strategy Engine
│   ├── strategy.py           ← Core indicators: Supertrend, EMA, RSI, VWAP
│   ├── signals_advanced.py   ← Advanced signals: PCR, OI, Greeks, IV Rank
│   │                            10-point confluence confidence score
│   ├── strategies.py         ← 6 strategies: TrendFollow, Scalp, Straddle,
│   │                            VWAPReversal, Breakout, IronCondor
│   └── strategy_selector.py  ← Auto-picks strategy by market regime + VIX
│
├── 🛡️ Risk Management
│   └── risk_engine.py        ← Trailing SL, position sizing (Kelly/fixed),
│                                event calendar, consecutive loss protection,
│                                blackout dates (RBI/Budget/holidays)
│
├── 💼 Order Execution
│   └── paper_trader.py       ← Paper + live order engine. Stores token,
│                                verifies fill, deducts costs, SQLite logging
│
├── 📊 Reporting
│   ├── dashboard.py          ← Flask web UI: live P&L, trade table, history
│   ├── report.py             ← CLI daily/weekly P&L report
│   └── backtester.py         ← Historical strategy backtesting engine
│
└── 🗄️ Data
    ├── data/trades.db        ← SQLite: all paper/live trades (auto-created)
    └── data/backtest.db      ← SQLite: backtest run results
```

---

## ⚙️ Prerequisites

### Option A — Podman Desktop (Free, Recommended)
```bash
brew install podman podman-compose
podman machine init && podman machine start
```

### Option B — Docker Desktop
Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)

> **Windows 11:** Use Git Bash or WSL2 terminal to run `run.sh`  
> **macOS:** Use Terminal directly

---

## 🔑 Angel One SmartAPI Setup (One-Time)

### Step 1 — Get API Key
1. Go to **[smartapi.angelone.in/new/apps](https://smartapi.angelone.in/new/apps)**
2. Login with your Angel One credentials
3. Click **"+ ADD APP"**
4. Fill in:
   - **App Name:** `NiftyBot`
   - **Redirect URL:** `https://niftybot.app` (any valid HTTPS domain)
   - **Primary Static IP:** Run `curl -s https://api.ipify.org` → paste your IP
5. Click **Add** → copy your **API Key**

### Step 2 — Enable TOTP
1. Go to **[smartapi.angelone.in](https://smartapi.angelone.in)** → **Enable TOTP** (top nav)
2. Scan QR with **Google Authenticator** app
3. Copy the **secret key** (16–32 uppercase letters + digits, e.g. `JBSWY3DPEHPK3PXP`)

### Step 3 — Note Your Credentials
```
API_KEY      → from smartapi.angelone.in/new/apps
CLIENT_ID    → your Angel One login ID (e.g. A123456)
PASSWORD     → your 4–6 digit Angel One MPIN
TOTP_SECRET  → base32 secret from Google Authenticator setup
```

---

## 🚀 Quick Start

```bash
# 1. Clone / download
git clone https://github.com/yourname/nifty_bot.git
cd nifty_bot

# 2. Configure credentials
cp .env.example .env
open -e .env          # macOS
# notepad .env        # Windows

# 3. Make scripts executable (macOS / Linux)
chmod +x run.sh entrypoint.sh

# 4. Build container image
./run.sh build

# 5. Start bot + dashboard
./run.sh start

# 6. Watch live logs
./run.sh logs

# 7. Open dashboard
open http://localhost:8080
```

---

## 🖥️ All Commands

```bash
./run.sh start      # Start bot + dashboard
./run.sh stop       # Stop all containers
./run.sh restart    # Restart bot only
./run.sh logs       # Tail live bot logs (Ctrl+C to exit)
./run.sh status     # Show container health + ports
./run.sh build      # Rebuild container image (after code changes)
./run.sh report     # Print today's P&L report to terminal
./run.sh shell      # Open bash shell inside container (debug)
```

---

## 📊 Trading Modes

### Paper Mode (Default — Safe)
```env
TRADING_MODE=paper
```
- Connects to SmartAPI for **live market data only**
- Runs all strategies and signals on real prices
- Deducts realistic costs (₹216/trade) from simulated P&L
- **Zero real orders placed**

### Live Mode (Real Money ⚠️)
```env
TRADING_MODE=live
```
- Places **real orders** via Angel One SmartAPI
- Verifies each order is filled before tracking P&L
- Only enable after **4+ weeks of profitable paper trading**
- Start with `MAX_LOTS_PER_TRADE=1` and `MAX_TRADES_PER_DAY=2`

---

## 🧠 Entry / Exit Algorithm

### ENTRY — Strategy Selector Flow
```
Every 30 seconds during market hours (9:15–15:30 IST):

  1. Fetch 5-min + 1-min Nifty candles + India VIX + option chain
  2. Detect market regime (TRENDING_UP / TRENDING_DOWN / SIDEWAYS)
  3. Check all risk gates (blackout day? daily limit? consecutive losses?)
  4. Select best strategy for current conditions:

     VIX > 20             → NO TRADE
     Expiry day morning   → SCALP (1-min EMA cross + volume)
     Pre-event day        → STRADDLE (buy CE + PE together)
     Sideways + VIX<14    → VWAP_REVERSAL or IRON_CONDOR
     Trending + breakout  → BREAKOUT
     Trending (default)   → TREND_FOLLOW

  5. Score signal across 10 indicators:
     Technical  (4 pts): Supertrend + EMA crossover + RSI + VWAP
     Sentiment  (3 pts): PCR + OI buildup + Max Pain
     Greeks     (3 pts): Delta range + IV Rank + Theta

  6. Score ≥ 6/10 → BUY CE (bullish) or PE (bearish)
  7. Check OI liquidity (OI > 100,000) before entering
```

### EXIT — First Condition Wins
```
  1. Hard Stop-Loss    → -10 pts from entry       → EXIT ❌
  2. Profit Target     → +20 pts from entry        → EXIT ✅
  3. Trailing SL       → activates at +12 pts,
                         trails 8 pts below peak   → EXIT ✅
  4. Supertrend flip   → trend reversed            → EXIT (early)
  5. EOD Force-exit    → 15:15 IST                 → EXIT
  6. Daily limit hit   → ₹1500 profit or ₹750 loss → no more trades
```

---

## 📉 Risk Management

| Parameter | Default | Description |
|---|---|---|
| `PROFIT_TARGET_POINTS` | 20 | Exit at +20 pts |
| `STOP_LOSS_POINTS` | 10 | Hard exit at -10 pts |
| `TRAIL_ACTIVATION_POINTS` | 12 | Trailing SL kicks in at +12 pts |
| `TRAIL_DISTANCE_POINTS` | 8 | Trails 8 pts below highest LTP |
| `DAILY_PROFIT_TARGET` | ₹1500 | Bot stops for day on reaching |
| `DAILY_MAX_LOSS` | ₹750 | Bot stops for day on hitting |
| `MAX_TRADES_PER_DAY` | 5 | Cap on daily trade count |
| `MAX_CONSECUTIVE_LOSSES` | 3 | Pause after 3 losses in a row |
| `MAX_LOTS_PER_TRADE` | 1 | Increase only after profitability proven |
| `SIZING_METHOD` | fixed_fractional | `kelly` or `half_kelly` for dynamic sizing |
| `VIX_MAX_THRESHOLD` | 20 | Skip all trading above this VIX |
| `COST_PER_TRADE` | ₹216 | Deducted from every trade P&L |
| `MIN_OI_THRESHOLD` | 100000 | Minimum OI to trade a strike |
| `EXIT_ALL_TIME` | 15:15 | Force-close all positions |

---

## 📋 Supported Instruments

| Instrument | Exchange | Lot Size | Strike Gap | Expiry | Cycle | Target / SL |
|---|---|---|---|---|---|---|
| **NIFTY 50** | NSE / NFO | 65 units | 50 pts | **Tuesday** | Weekly | 20 / 10 pts |
| **SENSEX** | BSE / BFO | 20 units | 100 pts | **Thursday** | Weekly | 25 / 12 pts |
| **BANKNIFTY** | NSE / NFO | 30 units | 100 pts | **Last Tuesday** | Monthly | 30 / 15 pts |
| **BANKEX** | BSE / BFO | 30 units | 100 pts | **Last Thursday** | Monthly | 30 / 15 pts |
| **FINNIFTY** | NSE / NFO | 60 units | 50 pts | **Last Tuesday** | Monthly | 20 / 10 pts |

> Lot sizes and expiry schedules verified against NSE/BSE circulars (2025-26).
> Override any lot size via env: `NIFTY_LOT_SIZE=65`, `FINNIFTY_LOT_SIZE=60` etc.
> Enable instruments: `INSTRUMENTS=NIFTY,SENSEX,BANKNIFTY` in `.env` (default: `NIFTY,SENSEX`).

---

## 🎯 6 Trading Strategies

| Strategy | Trigger | Best Conditions | Target / SL |
|---|---|---|---|
| **TREND_FOLLOW** | Supertrend+EMA+RSI+VWAP all align | Trending, VIX 12–20 | 20 / 10 pts |
| **SCALP** | 1-min EMA cross + volume surge | Expiry morning, high-VIX open | 8 / 5 pts |
| **STRADDLE** | Pre-event, IV cheap, VIX rising | Within 1 day of RBI/Budget | % of premium |
| **VWAP_REVERSAL** | Price deviates >0.5% from VWAP | Sideways, VIX < 18, post 10:30 | 15 / 8 pts |
| **BREAKOUT** | Close above/below 10-candle range | Post 9:45, after consolidation | Range × 0.5 |
| **IRON_CONDOR** | Sell OTM CE + PE (neutral) | Tuesday–Wednesday, VIX < 14 | Premium decay |

---

## 📡 Indicators Used

| Indicator | Calculation | Signal |
|---|---|---|
| **Supertrend** (7, 3) | ATR-based trend bands | UP/DOWN direction |
| **EMA Crossover** (9/21) | Exponential moving average | Fast > Slow = bullish |
| **RSI** (14) | Momentum oscillator | 45–65 = entry zone |
| **VWAP** | Volume-weighted avg price | Price > VWAP = bullish |
| **PCR** | Put OI ÷ Call OI | > 1.2 = bullish sentiment |
| **OI Analysis** | CE/PE OI buildup direction | PE buildup = bullish |
| **Max Pain** | Strike with max combined OI | Spot below = drift up |
| **Delta** | Rate of premium change | 0.35–0.65 = ideal range |
| **IV Rank** | Current IV vs 52-week range | < 30 = buy cheap premium |
| **Theta** | Daily premium decay | < 8 pts/day = acceptable |
| **India VIX** | Market fear index | > 20 = no trade |

---

## 🔌 Connection Flow

```
.env credentials
     │
     ▼
session_manager.py
  ├── SmartConnect(api_key)
  ├── pyotp.TOTP(secret).now()  ← generates 6-digit code
  ├── generateSession(id, pin, totp)
  ├── stores JWT + refreshToken
  ├── starts heartbeat thread (every 60s)
  │     ├── pings Nifty LTP to verify connection
  │     └── refreshes JWT every 6 hours
  └── auto-reconnects on any failure (exponential backoff)
        2s → 4s → 8s → 16s → 32s
```

---

## 🗄️ Trade Database Schema

All trades stored in `data/trades.db` (SQLite):

| Column | Type | Description |
|---|---|---|
| `date` | TEXT | Trade date (YYYY-MM-DD) |
| `entry_time` | TEXT | Entry timestamp (ISO) |
| `exit_time` | TEXT | Exit timestamp (ISO) |
| `trading_mode` | TEXT | `paper` or `live` |
| `symbol` | TEXT | e.g. `NIFTY26SEP2400CE` |
| `symbol_token` | TEXT | Angel One exchange token |
| `strike` | INTEGER | Strike price |
| `option_type` | TEXT | `CE` or `PE` |
| `lot_size` | INTEGER | Units per lot (65 for NIFTY, 20 for SENSEX) |
| `entry_price` | REAL | Premium at entry |
| `exit_price` | REAL | Premium at exit |
| `pnl_points` | REAL | Points gained/lost |
| `pnl_inr_gross` | REAL | Gross P&L in ₹ |
| `costs_inr` | REAL | Transaction costs deducted |
| `pnl_inr_net` | REAL | **Net P&L (what you actually make)** |
| `exit_reason` | TEXT | TARGET / STOPLOSS / TRAILING_SL / EOD |
| `order_id` | TEXT | Angel One order ID (live mode) |
| `confidence` | INTEGER | Signal confidence score (0–10) |
| `signal_detail` | TEXT | Strategy + indicators that triggered |

---

## 📊 Backtesting

```bash
# Open a shell inside the container first:
./run.sh shell

# ── Synthetic data (no credentials required) ──────────────────
# Quick test with 90 days of simulated Nifty-like data (GBM model)
python backtester.py --days 90 --strategy TREND_FOLLOW
python backtester.py --days 180 --all

# ── Real Angel One historical data ────────────────────────────
# Uses actual market candles via SmartAPI getCandleData endpoint.
# Requires valid .env credentials. Up to ~100 days of intraday data.
python backtester.py --days 90  --strategy TREND_FOLLOW --live-data
python backtester.py --days 90  --strategy SCALP        --live-data
python backtester.py --days 90  --all                   --live-data
python backtester.py --days 60  --all  --live-data  --instrument SENSEX

# Available strategies:
# TREND_FOLLOW | SCALP | VWAP_REVERSAL | BREAKOUT | STRADDLE | IRON_CONDOR
```

**Backtest report includes:**
- Win rate, total P&L, avg win/loss
- Max drawdown, Sharpe ratio, Profit Factor
- Verdict: ✅ Profitable / ⚠️ Marginal / ❌ Not profitable

> 💡 **Use `--live-data` for reliable results.** Synthetic data uses GBM and gives directional guidance only.
> Angel One provides up to ~100 days of intraday history via `getCandleData`. Falls back to synthetic automatically if login fails.

---

## 📅 Blackout Dates (No Trading)

Bot automatically skips trading on:
- **NSE Holidays** (Diwali, Republic Day, Independence Day etc.)
- **RBI Policy Days** (6 per year — rate announcements cause gap moves)
- **Budget Day** (extreme volatility)
- **Weekends** (NSE closed)

Update `BLACKOUT_DATES` in [`risk_engine.py`](risk_engine.py) annually.

**Caution Days** (trade at 50% size):
- Day before/after major events (configurable in `CAUTION_DATES`)

---

## 🔐 Security

- `.env` is **excluded from Docker image** via `.dockerignore` ✅
- Bot runs as **non-root user** (`botuser`) inside container ✅
- Data persists in **named volumes** — survives restarts ✅
- `TRADING_MODE=paper` is the **hard default** ✅
- Live orders require **explicit** `TRADING_MODE=live` ✅
- Order fill **verified** before P&L is tracked ✅
- Empty `symboltoken` **aborts** live order (no silent rejections) ✅

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---|---|
| `Login failed: Non-base32 digit found` | `TOTP_SECRET` has invalid chars — must be uppercase A-Z and 2-7 only |
| `Login failed: Invalid token` | Wrong `CLIENT_ID` or `PASSWORD` in `.env` |
| `symboltoken is empty` | Token lookup failed — check internet, try `./run.sh restart` |
| `Order REJECTED` | Insufficient margin, or market closed — check Angel One app |
| `Order fill timeout` | Increase `FILL_TIMEOUT_SECS` or switch to `LIMIT` order type |
| Dashboard not loading | Check port 8080 free: `lsof -i :8080` |
| `PermissionError: /app/logs` | Volumes mounted as root — bot falls back to stdout logging automatically |
| `ModuleNotFoundError: logzero` | Rebuild image: `./run.sh build` |
| Container crashes instantly | Run `podman logs nifty_bot` to see error |
| `No trades generated` in backtest | Increase `--days` or lower `MIN_SIGNAL_CONFIDENCE` |

---

## 📅 Recommended Roadmap to Live Trading

```
Week 1–2  →  Paper trade every market day (Mon–Fri, 9:20–14:30 IST)
             Check dashboard win rate daily
             Target: understand the signals, learn when bot trades

Week 3–4  →  Analyse paper trades in dashboard
             Win rate > 50% net? → proceed
             Win rate < 40%?     → tune MIN_SIGNAL_CONFIDENCE, VIX threshold

Week 5    →  Run backtest on 90 days (./run.sh shell → python backtester.py)
             Sharpe > 1.0 and Profit Factor > 1.5? → ready for live

Week 6    →  Switch TRADING_MODE=live
             Set MAX_TRADES_PER_DAY=2, MAX_LOTS_PER_TRADE=1
             Monitor first 5 live trades manually in Angel One app

Week 7+   →  Scale up gradually only after 4 profitable live weeks
             Never increase lots during a losing streak
```

---

## ⚠️ Disclaimer

> This bot is for **educational and research purposes only**.  
> F&O trading involves **substantial financial risk** and is not suitable for everyone.  
> Past strategy performance does **not** guarantee future profits.  
> Always paper trade for a minimum of **4 weeks** before using real capital.  
> The authors are **not responsible** for any financial losses.  
> Ensure compliance with **SEBI regulations** before deploying automated trading systems.  
> Never trade with money you cannot afford to lose.

---

## 🔧 Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Broker API | Angel One SmartAPI v1.5.5 |
| Authentication | TOTP via pyotp |
| Data Analysis | pandas, numpy |
| Web Dashboard | Flask 3.1 |
| Database | SQLite (via sqlite3) |
| Logging | loguru |
| Container | Podman / Docker |
| Orchestration | podman-compose / docker-compose |

---

*NiftyBot v2 · Python · Angel One SmartAPI · Podman · SQLite*
