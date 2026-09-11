"""
dashboard.py — Real-time NiftyBot P&L + Signals Dashboard.

Endpoints:
  GET /           → Main dashboard page (HTML shell, data via JS fetch)
  GET /api/live   → Live state JSON (LTP, floating P&L, signals) — polls every 2s
  GET /api/summary → Today's closed-trade summary JSON
  GET /api/history → Last 30 days P&L JSON
  GET /health     → Health check

Architecture:
  - bot (main.py) writes  data/live_state.json  on every 30s tick
  - dashboard reads that file on every /api/live request
  - JS in the browser polls /api/live every 2 seconds → no page reload needed
  - Closed-trade P&L comes from SQLite trades.db (same volume mount)
"""

import os
import json
from flask import Flask, render_template_string, jsonify
from datetime import date, timedelta
from loguru import logger

import config
from paper_trader import get_daily_summary

app = Flask(__name__)

_LIVE_STATE_PATH = os.path.join(config.DATA_DIR, "live_state.json")


def _read_live_state() -> dict:
    """Read the live_state.json written by main.py on each tick."""
    try:
        with open(_LIVE_STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"timestamp": "", "vix": 0.0, "regime": "BOT NOT RUNNING", "instruments": {}}
    except Exception as exc:
        logger.warning(f"live_state read error: {exc}")
        return {"timestamp": "", "vix": 0.0, "regime": "READ ERROR", "instruments": {}}


# ── HTML shell — data loaded via fetch() every 2 seconds ──────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NiftyBot Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:       #0d1117;
      --surface:  #161b22;
      --border:   #30363d;
      --muted:    #8b949e;
      --text:     #e6edf3;
      --green:    #3fb950;
      --red:      #f85149;
      --blue:     #58a6ff;
      --yellow:   #d29922;
      --purple:   #bc8cff;
      --dim:      #484f58;
    }
    body { font-family: -apple-system,"Segoe UI",system-ui,sans-serif; background:var(--bg); color:var(--text); font-size:14px; line-height:1.5; }

    /* ── Header ── */
    header { background:var(--surface); border-bottom:1px solid var(--border); padding:12px 24px; display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:100; }
    header h1 { font-size:18px; font-weight:700; color:var(--blue); }
    .header-right { display:flex; align-items:center; gap:16px; }
    .mode-badge { padding:3px 12px; border-radius:20px; font-size:11px; font-weight:700; border:1px solid; }
    .mode-paper { background:#0d2137; color:var(--blue); border-color:var(--blue); }
    .mode-live  { background:#3d0000; color:var(--red);  border-color:var(--red); }
    #pulse { width:8px; height:8px; border-radius:50%; background:var(--dim); display:inline-block; }
    #pulse.alive { background:var(--green); box-shadow:0 0 6px var(--green); animation:blink 2s infinite; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.4} }
    #last-update { font-size:11px; color:var(--dim); }

    /* ── Layout ── */
    .container { max-width:1280px; margin:0 auto; padding:20px 24px; }

    /* ── Top KPI cards ── */
    .kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:24px; }
    .kpi { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px 18px; }
    .kpi .kpi-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }
    .kpi .kpi-value { font-size:26px; font-weight:700; }
    .kpi .kpi-sub   { font-size:11px; color:var(--muted); margin-top:4px; }

    /* ── Instrument panels — two-column grid ── */
    .inst-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(580px,1fr)); gap:16px; margin-bottom:24px; }
    .inst-panel { background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
    .inst-header { padding:10px 16px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
    .inst-name { font-size:15px; font-weight:700; color:var(--text); }
    .inst-spot { font-size:20px; font-weight:700; }
    .inst-body { padding:14px 16px; display:grid; grid-template-columns:1fr 1fr; gap:14px; }

    /* ── Position box ── */
    .pos-box { border:1px solid var(--border); border-radius:6px; padding:12px; grid-column:1/-1; }
    .pos-box.in-trade { border-color:var(--blue); background:#0a1929; }
    .pos-box.flat      { border-color:var(--dim); }
    .pos-title { font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.05em; margin-bottom:8px; }
    .pos-symbol { font-size:16px; font-weight:700; margin-bottom:4px; }
    .pos-pnl { font-size:28px; font-weight:700; }
    .pos-meta { font-size:12px; color:var(--muted); margin-top:6px; }

    /* ── Signal grid (4 indicator pills) ── */
    .sig-section { grid-column:1/-1; }
    .sig-title { font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.05em; margin-bottom:8px; }
    .sig-pills { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
    .pill { padding:3px 10px; border-radius:4px; font-size:11px; font-weight:700; }
    .pill-green  { background:#0d3321; color:var(--green); }
    .pill-red    { background:#3d0000; color:var(--red); }
    .pill-blue   { background:#0d1b2e; color:var(--blue); }
    .pill-yellow { background:#2a1f00; color:var(--yellow); }
    .pill-grey   { background:#21262d; color:var(--muted); }
    .pill-purple { background:#1e0f3d; color:var(--purple); }

    .conf-bar { display:flex; gap:4px; align-items:center; margin-top:6px; }
    .conf-dot { width:14px; height:14px; border-radius:2px; background:var(--dim); }
    .conf-dot.filled-green  { background:var(--green); }
    .conf-dot.filled-yellow { background:var(--yellow); }
    .conf-dot.filled-red    { background:var(--red); }
    .conf-label { font-size:11px; color:var(--muted); margin-left:4px; }

    /* ── Stats row ── */
    .stat-row { display:flex; gap:20px; flex-wrap:wrap; }
    .stat-item { display:flex; flex-direction:column; }
    .stat-label { font-size:10px; text-transform:uppercase; color:var(--muted); }
    .stat-val   { font-size:14px; font-weight:600; }

    /* ── Trades table ── */
    .section-title { font-size:15px; font-weight:600; color:var(--text); margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border); }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    th,td { padding:8px 12px; text-align:left; border-bottom:1px solid #1c2128; }
    th { background:var(--surface); color:var(--muted); font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:.04em; }
    tr:hover td { background:#1c2128; }
    .badge { padding:2px 7px; border-radius:3px; font-size:10px; font-weight:700; }

    /* ── History strip ── */
    .hist-strip { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
    .hist-cell { background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:10px 12px; min-width:100px; text-align:center; }
    .hist-date { font-size:10px; color:var(--muted); margin-bottom:2px; }
    .hist-pnl  { font-size:15px; font-weight:700; }
    .hist-cnt  { font-size:10px; color:var(--muted); }

    /* ── Colours ── */
    .green { color:var(--green); } .red { color:var(--red); } .blue { color:var(--blue); }
    .grey  { color:var(--muted); } .yellow { color:var(--yellow); }

    footer { margin-top:32px; text-align:center; color:var(--dim); font-size:11px; border-top:1px solid var(--border); padding:16px; }
  </style>
</head>
<body>

<header>
  <h1>📈 NiftyBot</h1>
  <div class="header-right">
    <span id="pulse"></span>
    <span id="last-update">Connecting…</span>
    <span id="vix-badge" class="pill pill-grey">VIX —</span>
    <span id="regime-badge" class="pill pill-grey">—</span>
    <span class="mode-badge {{ 'mode-live' if mode=='live' else 'mode-paper' }}">{{ mode|upper }} MODE</span>
  </div>
</header>

<div class="container">

  <!-- ── KPI Row ───────────────────────────────────────────── -->
  <div class="kpi-grid" id="kpi-row">
    <div class="kpi">
      <div class="kpi-label">Today's Net P&amp;L</div>
      <div class="kpi-value grey" id="kpi-pnl">₹0</div>
      <div class="kpi-sub" id="kpi-pnl-sub">Loading…</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Floating P&amp;L</div>
      <div class="kpi-value grey" id="kpi-float">₹0</div>
      <div class="kpi-sub" id="kpi-float-sub">No open position</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Trades Today</div>
      <div class="kpi-value blue" id="kpi-trades">—</div>
      <div class="kpi-sub" id="kpi-wl">— W / — L</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Daily Target</div>
      <div class="kpi-value grey" id="kpi-target">₹{{ target|int }}</div>
      <div class="kpi-sub" id="kpi-target-sub">—</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Max Loss Cap</div>
      <div class="kpi-value grey" id="kpi-cap">₹{{ max_loss|int }}</div>
      <div class="kpi-sub" id="kpi-cap-sub">—</div>
    </div>
  </div>

  <!-- ── Per-Instrument Live Panels ───────────────────────── -->
  <div class="inst-grid" id="inst-grid">
    <!-- filled by JS -->
  </div>

  <!-- ── Today's Trades Table ─────────────────────────────── -->
  <div class="section-title" id="trades-title">📋 Today's Trades — {{ today }}</div>
  <div id="trades-wrap">
    <p class="grey" style="padding:16px 0">Loading trades…</p>
  </div>

  <!-- ── P&L History ──────────────────────────────────────── -->
  <div class="section-title" style="margin-top:32px;">📅 Recent P&amp;L History</div>
  <div class="hist-strip" id="hist-strip">
    <p class="grey">Loading…</p>
  </div>

</div>

<footer>NiftyBot · {{ mode|upper }} MODE · {{ today }} · Angel One SmartAPI</footer>

<script>
// ── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmtINR = v => {
  const abs = Math.abs(v), sign = v >= 0 ? '+' : '-';
  return sign + '₹' + abs.toLocaleString('en-IN', {maximumFractionDigits:0});
};
const fmtPts = v => (v >= 0 ? '+' : '') + v.toFixed(2) + ' pts';
const cls = v => v > 0 ? 'green' : v < 0 ? 'red' : 'grey';

// ── Pill factory ─────────────────────────────────────────────
function pill(label, colour) {
  return `<span class="pill pill-${colour}">${label}</span>`;
}

// ── Confidence bar ───────────────────────────────────────────
function confBar(score, max) {
  let dots = '';
  for (let i = 0; i < max; i++) {
    let c = 'conf-dot';
    if (i < score) {
      c += score >= 7 ? ' filled-green' : score >= 4 ? ' filled-yellow' : ' filled-red';
    }
    dots += `<div class="${c}"></div>`;
  }
  const colour = score >= 7 ? 'green' : score >= 4 ? 'yellow' : 'red';
  return `<div class="conf-bar">${dots}<span class="conf-label ${colour}">${score}/${max}</span></div>`;
}

// ── Render one instrument panel ──────────────────────────────
function renderInst(name, d) {
  const spotColour = 'blue';
  const posClass   = d.status === 'IN_TRADE' ? 'in-trade' : 'flat';

  // Position box
  let posHTML = '';
  if (d.status === 'IN_TRADE') {
    const fpColour = cls(d.floating_pnl_inr);
    const fpPts    = fmtPts(d.floating_pnl);
    const fpInr    = fmtINR(d.floating_pnl_inr);
    posHTML = `
      <div class="pos-box in-trade">
        <div class="pos-title">Open Position</div>
        <div class="pos-symbol">${d.symbol} <span class="pill ${d.option_type==='CE'?'pill-green':'pill-red'}">${d.option_type}</span></div>
        <div class="pos-pnl ${fpColour}">${fpInr}</div>
        <div class="pos-meta">
          Entry ₹${d.entry_price.toFixed(2)} → LTP ₹${d.ltp.toFixed(2)}
          &nbsp;|&nbsp; ${fpPts}
        </div>
      </div>`;
  } else {
    posHTML = `
      <div class="pos-box flat">
        <div class="pos-title">Position</div>
        <div class="pos-symbol grey">FLAT</div>
        <div class="pos-meta grey">No open trade</div>
      </div>`;
  }

  // Indicator pills
  const stPill  = d.supertrend === 'UP'   ? pill('ST ▲ UP',   'green')  : pill('ST ▼ DOWN', 'red');
  const emaPill = d.ema_cross  === 'BULL' ? pill('EMA BULL',  'green')  : pill('EMA BEAR',  'red');
  const rsiColour = d.rsi > 70 ? 'red' : d.rsi < 30 ? 'yellow' : 'green';
  const rsiPill  = pill(`RSI ${d.rsi}`, rsiColour);
  const vwapPill = d.vwap_pos === 'ABOVE' ? pill('VWAP ▲', 'green') : pill('VWAP ▼', 'red');
  const pcrPill  = d.pcr  ? pill(`PCR ${d.pcr.toFixed(2)}`, d.pcr >= 1.2 ? 'green' : d.pcr <= 0.8 ? 'red' : 'grey') : '';
  const mpPill   = d.max_pain ? pill(`Max Pain ${d.max_pain}`, 'purple') : '';

  // Strategy + regime
  const regimePill  = d.regime   ? pill(d.regime,   'grey')   : '';
  const stratPill   = d.strategy && d.strategy !== '—' ? pill(d.strategy, 'blue') : '';

  return `
  <div class="inst-panel">
    <div class="inst-header">
      <span class="inst-name">${name}</span>
      <span class="inst-spot ${spotColour}">${d.spot ? d.spot.toLocaleString('en-IN') : '—'}</span>
    </div>
    <div class="inst-body">
      ${posHTML}

      <div class="stat-row" style="grid-column:1/-1">
        <div class="stat-item">
          <span class="stat-label">Daily P&amp;L</span>
          <span class="stat-val ${cls(d.daily_pnl)}">${fmtINR(d.daily_pnl || 0)}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Trades</span>
          <span class="stat-val blue">${d.trade_count || 0}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Regime</span>
          <span class="stat-val">${regimePill}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Strategy</span>
          <span class="stat-val">${stratPill || '<span class="grey">—</span>'}</span>
        </div>
      </div>

      <div class="sig-section">
        <div class="sig-title">Live Signals</div>
        <div class="sig-pills">${stPill} ${emaPill} ${rsiPill} ${vwapPill} ${pcrPill} ${mpPill}</div>
        ${d.confidence ? confBar(d.confidence, 10) : ''}
        ${d.signal_reason && d.signal_reason !== '—'
          ? `<div style="font-size:11px;color:var(--muted);margin-top:6px;opacity:.8">${d.signal_reason}</div>`
          : ''}
      </div>
    </div>
  </div>`;
}

// ── Render trades table ───────────────────────────────────────
function renderTrades(trades) {
  if (!trades || !trades.length) {
    return '<p class="grey" style="padding:16px 0">No trades recorded today.</p>';
  }
  let rows = trades.map((t, i) => {
    const pnl     = t.pnl_inr_net ?? t.pnl_inr ?? null;
    const pnlPts  = t.pnl_points ?? null;
    const colour  = pnl === null ? 'grey' : cls(pnl);
    const exReason= t.exit_reason || '';
    const erClass = exReason.includes('TARGET') ? 'pill-green'
                  : exReason.includes('STOP') || exReason.includes('LOSS') ? 'pill-red'
                  : exReason ? 'pill-grey' : 'pill-blue';
    const erLabel = exReason || 'OPEN';
    return `<tr>
      <td class="grey">${i+1}</td>
      <td>${t.symbol}</td>
      <td><span class="badge ${t.option_type==='CE'?'pill-green':'pill-red'}">${t.option_type}</span></td>
      <td class="grey">${(t.entry_time||'').slice(11,16) || '—'}</td>
      <td>₹${(t.entry_price||0).toFixed(2)}</td>
      <td>${t.exit_price ? '₹'+t.exit_price.toFixed(2) : '<span class="blue">OPEN</span>'}</td>
      <td class="${colour}">${pnlPts !== null ? (pnlPts>=0?'+':'')+pnlPts.toFixed(1)+' pts' : '—'}</td>
      <td class="${colour}">${pnl !== null ? fmtINR(pnl) : '—'}</td>
      <td><span class="badge ${erClass}">${erLabel}</span></td>
      <td class="grey">${t.confidence ?? '—'}/10</td>
    </tr>`;
  }).join('');
  return `<table>
    <thead><tr>
      <th>#</th><th>Symbol</th><th>Type</th><th>Entry</th>
      <th>Entry ₹</th><th>Exit ₹</th><th>P&amp;L pts</th><th>P&amp;L ₹</th>
      <th>Exit Reason</th><th>Conf</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div style="font-size:11px;color:var(--dim);text-align:right;margin-top:6px">
    Live data · updates every 2s
  </div>`;
}

// ── Render history strip ─────────────────────────────────────
function renderHistory(history) {
  return history.map(h => {
    const c = cls(h.total_pnl);
    return `<div class="hist-cell">
      <div class="hist-date">${h.date.slice(5)}</div>
      <div class="hist-pnl ${c}">${fmtINR(h.total_pnl)}</div>
      <div class="hist-cnt">${h.count} trades</div>
    </div>`;
  }).join('');
}

// ── Poll /api/live every 2s ───────────────────────────────────
let lastTs = '';
let historyLoaded = false;

async function pollLive() {
  try {
    const [liveResp, summResp] = await Promise.all([
      fetch('/api/live'),
      fetch('/api/summary'),
    ]);
    const live = await liveResp.json();
    const summ = await summResp.json();

    // ── Header pulse + timestamp ───────────────────────────
    const pulse = $('pulse');
    if (live.timestamp && live.timestamp !== lastTs) {
      pulse.className = 'alive';
      lastTs = live.timestamp;
    }
    $('last-update').textContent = live.timestamp
      ? 'Updated ' + live.timestamp.slice(11,19)
      : 'Waiting for bot…';

    // ── VIX badge ─────────────────────────────────────────
    const vixEl = $('vix-badge');
    if (live.vix) {
      const vv = live.vix;
      vixEl.textContent = 'VIX ' + vv.toFixed(1);
      vixEl.className = 'pill ' + (vv > 20 ? 'pill-red' : vv > 15 ? 'pill-yellow' : 'pill-green');
    }

    // ── Regime badge ──────────────────────────────────────
    const regEl = $('regime-badge');
    if (live.regime) {
      regEl.textContent = live.regime;
      regEl.className = 'pill ' + (
        live.regime === 'TRENDING_UP'   ? 'pill-green'  :
        live.regime === 'TRENDING_DOWN' ? 'pill-red'    :
        live.regime === 'SIDEWAYS'      ? 'pill-yellow' : 'pill-grey'
      );
    }

    // ── KPI cards ─────────────────────────────────────────
    const pnl = summ.total_pnl || 0;
    $('kpi-pnl').textContent = fmtINR(pnl);
    $('kpi-pnl').className   = 'kpi-value ' + cls(pnl);
    $('kpi-pnl-sub').textContent = summ.target_hit ? '🎯 Target reached!' : summ.limit_hit ? '🛑 Loss limit hit' : `${summ.count} trades closed`;

    // Floating P&L = sum of all open positions across instruments
    let totalFloat = 0, totalFloatInr = 0;
    Object.values(live.instruments || {}).forEach(d => {
      if (d.status === 'IN_TRADE') {
        totalFloat    += d.floating_pnl    || 0;
        totalFloatInr += d.floating_pnl_inr || 0;
      }
    });
    $('kpi-float').textContent = fmtINR(totalFloatInr);
    $('kpi-float').className   = 'kpi-value ' + cls(totalFloatInr);
    $('kpi-float-sub').textContent = totalFloatInr !== 0
      ? fmtPts(totalFloat) + ' · live'
      : 'No open position';

    $('kpi-trades').textContent = summ.count ?? '—';
    $('kpi-wl').textContent     = `${summ.wins} W / ${summ.losses} L`;

    const targetPct = pnl / {{ target }} * 100;
    $('kpi-target-sub').textContent = `${Math.min(targetPct,100).toFixed(0)}% of target`;
    $('kpi-target').className = pnl >= {{ target }} ? 'kpi-value green' : 'kpi-value grey';

    const lossPct = Math.abs(Math.min(pnl,0)) / {{ max_loss }} * 100;
    $('kpi-cap-sub').textContent = `${lossPct.toFixed(0)}% of cap`;
    $('kpi-cap').className = pnl <= -{{ max_loss }} ? 'kpi-value red' : 'kpi-value grey';

    // ── Instrument panels ─────────────────────────────────
    const instGrid = $('inst-grid');
    const insts = live.instruments || {};
    if (Object.keys(insts).length === 0) {
      instGrid.innerHTML = '<p class="grey" style="padding:16px">Bot not running yet — start with <code>./run.sh start</code></p>';
    } else {
      instGrid.innerHTML = Object.entries(insts).map(([n,d]) => renderInst(n, d)).join('');
    }

    // ── Trades table ──────────────────────────────────────
    $('trades-wrap').innerHTML = renderTrades(summ.trades || []);

  } catch(e) {
    $('pulse').className = '';
    $('last-update').textContent = 'Connection error — retrying…';
  }
}

// ── Load history once (changes rarely) ──────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const h = await r.json();
    $('hist-strip').innerHTML = renderHistory(h.slice(0, 20));
  } catch(e) {
    $('hist-strip').innerHTML = '<span class="grey">History unavailable</span>';
  }
}

// ── Boot ─────────────────────────────────────────────────────
pollLive();
loadHistory();
setInterval(pollLive, 2000);
setInterval(loadHistory, 60000);
</script>
</body>
</html>
"""


# ── API Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(
        _HTML,
        today   = date.today().isoformat(),
        mode    = config.TRADING_MODE,
        target  = config.DAILY_PROFIT_TARGET,
        max_loss= config.DAILY_MAX_LOSS,
    )


@app.route("/api/live")
def api_live():
    """Live state: spot, floating P&L, signals — from live_state.json written by bot."""
    return jsonify(_read_live_state())


@app.route("/api/summary")
def api_summary():
    """Today's closed-trade summary from SQLite."""
    return jsonify(get_daily_summary())


@app.route("/api/history")
def api_history():
    """Last 30 days P&L summary."""
    history = []
    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        history.append(get_daily_summary(d))
    return jsonify(history)


# Legacy routes kept for backward compatibility
@app.route("/trades")
def trades_json():
    return jsonify(get_daily_summary()["trades"])


@app.route("/summary")
def summary_json():
    return jsonify(get_daily_summary())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "mode": config.TRADING_MODE})


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"🌐 Dashboard starting on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    app.run(
        host        = config.DASHBOARD_HOST,
        port        = config.DASHBOARD_PORT,
        debug       = False,
        use_reloader= False,
        threaded    = True,   # allow concurrent API requests from JS polling
    )
