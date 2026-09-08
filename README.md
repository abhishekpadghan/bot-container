# 📈 NiftyBot — Nifty F&O Paper / Live Trading Bot

> Automated trading bot for **Nifty 50 Options** via **Angel One SmartAPI**.
> Default mode is **paper trading** — no real orders are placed until you explicitly switch to live.

---

## 🎯 Goal

| Target | Value |
|---|---|
| Daily Profit Target | ₹1,500 (+20 points × 75 lot size) |
| Max Loss per Trade | ₹750 (-10 points × 75 lot size) |
| Risk : Reward | 1 : 2 |
| Instrument | Nifty 50 Options (CE / PE) |
| Lot Size | 75 units (verify current NSE lot size before use) |

---

## 🏗️ Architecture

```
nifty_bot/
├── Dockerfile            ← Python 3.11-slim image, IST timezone, non-root user
├── podman-compose.yml    ← 3 services: bot, dashboard, cli
├── run.sh                ← One-command wrapper (start/stop/logs/report/shell)
├── .env.example          ← Template for API credentials & config
├── .dockerignore         ← Keeps secrets & cache out of image
├── requirements.txt      ← All Python dependencies (pinned versions)
├── config.py             ← Loads .env settings
├── auth.py               ← Angel One SmartAPI login + TOTP
├── market_data.py        ← Live LTP, OI, Greeks via WebSocket
├── strategy.py           ← Entry/exit signal logic (Supertrend, RSI, VWAP)
├── paper_trader.py       ← Simulated order execution + SQLite logging
├── main.py               ← Bot entry point / main loop
├── dashboard.py          ← Flask web UI for P&L monitoring
├── report.py             ← CLI P&L report generator
└── data/
    └── trades.db         ← SQLite database (auto-created, persisted via volume)
```

---

## ⚙️ Prerequisites

### Option A — Docker Desktop (Windows 11)
1. Download & install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)**
2. Start Docker Desktop
3. Open **PowerShell** or **Git Bash**

### Option B — Podman Desktop (Windows 11)
1. Download & install **[Podman Desktop](https://podman-desktop.io/)**
2. Start Podman Desktop / initialize the Podman machine
3. Open **PowerShell** or **Git Bash**

> ✅ Both work identically. `run.sh` auto-detects which one you have installed.

---

## 🔑 Angel One SmartAPI Setup (One-Time)

### Step 1 — Register for SmartAPI
1. Go to **[smartapi.angelbroking.com](https://smartapi.angelbroking.com)**
2. Login with your Angel One Client ID & password
3. Click **"Create New App"**
4. Copy your **API Key**

### Step 2 — Enable TOTP (Two-Factor Auth)
1. Open **Angel One mobile app**
2. Go to **Profile → Security Settings → Enable TOTP**
3. Scan the QR code with **Google Authenticator** or **Authy**
4. Copy the **TOTP secret key** shown during setup (save it safely!)

### Step 3 — Note your credentials
```
API_KEY      → from smartapi.angelbroking.com
CLIENT_ID    → your Angel One login ID
PASSWORD     → your Angel One MPIN (4–6 digit PIN)
TOTP_SECRET  → secret key from Google Authenticator setup
```

---

## 🚀 Quick Start

### 1. Clone / Download the project
```bash
git clone https://github.com/yourname/nifty_bot.git
cd nifty_bot
```

### 2. Configure your credentials
```bash
cp .env.example .env
```
Open `.env` in any text editor (Notepad, VS Code) and fill in:
```env
API_KEY=your_api_key_here
CLIENT_ID=your_angel_one_client_id
PASSWORD=your_mpin_here
TOTP_SECRET=your_totp_secret_from_google_authenticator
TRADING_MODE=paper
```
> ⚠️ **Never share or commit your `.env` file. It contains your credentials.**

### 3. Make run.sh executable (Git Bash / WSL / Linux / Mac)
```bash
chmod +x run.sh
```

### 4. Build the container image
```bash
./run.sh build
```

### 5. Start the bot
```bash
./run.sh start
```

### 6. Open the dashboard
Visit **[http://localhost:8080](http://localhost:8080)** in your browser to see live P&L.

---

## 🖥️ All Commands

```bash
./run.sh start      # Start bot + dashboard (paper mode by default)
./run.sh stop       # Stop all containers
./run.sh restart    # Restart the bot only
./run.sh logs       # Tail live logs (Ctrl+C to exit)
./run.sh status     # Show container health & status
./run.sh build      # Rebuild the Docker/Podman image
./run.sh report     # Print today's P&L summary report
./run.sh shell      # Open bash shell inside the container (debug)
```

---

## 📊 Trading Modes

### Paper Mode (Default — Recommended for beginners)
```env
TRADING_MODE=paper
```
- Connects to SmartAPI for **live market data only**
- Calculates entry/exit signals using real prices
- Logs simulated trades to `data/trades.db`
- **Does NOT place any real orders**

### Live Mode (⚠️ Real money — use with extreme caution)
```env
TRADING_MODE=live
```
- Places **real orders** through Angel One SmartAPI
- Only enable after at least **4 weeks of profitable paper trading**
- Ensure SEBI algo trading compliance before going live

---

## 📉 Risk Management

| Parameter | Default | Description |
|---|---|---|
| `PROFIT_TARGET_POINTS` | 20 | Exit trade at +20 points |
| `STOP_LOSS_POINTS` | 10 | Exit trade at -10 points |
| `DAILY_PROFIT_TARGET` | ₹1500 | Bot stops for the day on reaching target |
| `DAILY_MAX_LOSS` | ₹750 | Bot stops for the day on hitting max loss |
| `MAX_TRADES_PER_DAY` | 5 | Max number of trades per day |
| `VIX_MAX_THRESHOLD` | 20 | Skip trading when India VIX exceeds this |
| `EXIT_ALL_TIME` | 15:15 | Force-close all open positions |

All values are configurable in your `.env` file.

---

## 📡 Indicators Used for Signals

| Indicator | Purpose |
|---|---|
| **Supertrend** (7, 3) | Primary trend direction — Buy/Sell signal |
| **RSI** (14) | Overbought/Oversold filter |
| **EMA Crossover** (9/21) | Trend confirmation |
| **VWAP** | Intraday fair value reference |
| **India VIX** | Volatility filter — avoids high-risk sessions |
| **PCR (Put-Call Ratio)** | Sentiment filter |

---

## 🗄️ Trade Logs & Database

All (paper) trades are saved to `data/trades.db` (SQLite):

```sql
SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10;
```

| Column | Description |
|---|---|
| `timestamp` | Trade entry time (IST) |
| `action` | BUY / SELL |
| `symbol` | e.g. NIFTY23SEP19800CE |
| `strike` | Strike price |
| `option_type` | CE or PE |
| `entry_price` | Premium at entry |
| `exit_price` | Premium at exit |
| `pnl_points` | +/- points |
| `pnl_inr` | +/- ₹ (points × lot size) |
| `exit_reason` | TARGET / STOPLOSS / EOD |

---

## 🔐 Security Notes

- Your `.env` file is **excluded from the Docker image** via `.dockerignore`
- The bot runs as a **non-root user** inside the container
- Trade data is stored in a **named Docker volume** — survives container restarts
- `TRADING_MODE=paper` is the **default** — live trading requires explicit opt-in

---

## ⚠️ Disclaimer

> This bot is for **educational and research purposes only**.
> Trading in F&O instruments involves substantial financial risk.
> Past performance of any strategy does **not** guarantee future results.
> Always paper trade for a minimum of 4 weeks before using real capital.
> The authors are not responsible for any financial losses.
> Ensure compliance with **SEBI regulations** before deploying any automated trading system.

---

## 📞 Troubleshooting

| Problem | Solution |
|---|---|
| `Login failed` | Check `CLIENT_ID`, `PASSWORD`, `TOTP_SECRET` in `.env` |
| `TOTP invalid` | Ensure system clock is synced (TOTP is time-sensitive) |
| `Container won't start` | Run `./run.sh logs` to see the error |
| `Dashboard not opening` | Check port 8080 is free: `netstat -an \| grep 8080` |
| `trades.db not found` | First trade hasn't been made yet — bot creates it on first entry |
| `podman: command not found` | Install Podman Desktop or Docker Desktop |

---

## 📅 Recommended Workflow

```
Week 1–2  →  Paper trade, observe signals, tweak parameters in .env
Week 3–4  →  Validate consistent profit in paper mode
Week 5+   →  Switch to TRADING_MODE=live with minimal capital first
```

---

*Built with ❤️ using Python · Angel One SmartAPI · Podman · SQLite*
