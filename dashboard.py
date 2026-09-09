"""
dashboard.py — Flask web dashboard for NiftyBot P&L monitoring.

Endpoints:
  GET /           → Today's summary page
  GET /trades     → All trades as JSON (for AJAX refresh)
  GET /summary    → Daily summary as JSON
  GET /history    → Last 10 days of P&L history

Auto-refreshes every 30 seconds via meta tag.
No JavaScript frameworks — pure HTML + CSS, works on any device.
"""

from flask import Flask, render_template_string, jsonify
from datetime import date, timedelta
from loguru import logger

import config
from paper_trader import get_daily_summary, get_db

app = Flask(__name__)

# ── HTML Template ─────────────────────────────────────────────

_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>NiftyBot Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
      background: #0f1117; color: #e6edf3; font-size: 14px; line-height: 1.6;
    }
    header {
      background: #161b22; border-bottom: 1px solid #30363d;
      padding: 16px 24px; display: flex; justify-content: space-between;
      align-items: center;
    }
    header h1 { font-size: 20px; font-weight: 600; color: #58a6ff; }
    header .mode {
      padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
      background: {% if mode=='live' %}#3d0000{% else %}#0d2137{% endif %};
      color: {% if mode=='live' %}#ff7b7b{% else %}#58a6ff{% endif %};
      border: 1px solid {% if mode=='live' %}#ff7b7b{% else %}#58a6ff{% endif %};
    }
    .container { max-width: 1100px; margin: 0 auto; padding: 24px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }
    .card {
      background: #161b22; border: 1px solid #30363d; border-radius: 8px;
      padding: 20px; text-align: center;
    }
    .card .label { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
    .card .value { font-size: 28px; font-weight: 700; }
    .green { color: #3fb950; }
    .red   { color: #f85149; }
    .blue  { color: #58a6ff; }
    .grey  { color: #8b949e; }
    table  { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #21262d; font-size: 13px; }
    th     { background: #161b22; color: #8b949e; font-weight: 600; text-transform: uppercase; font-size: 11px; }
    tr:hover td { background: #161b22; }
    .badge {
      padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
    }
    .badge-green { background: #0d3321; color: #3fb950; }
    .badge-red   { background: #3d0000; color: #f85149; }
    .badge-blue  { background: #0d1b2e; color: #58a6ff; }
    .badge-grey  { background: #21262d; color: #8b949e; }
    .section-title {
      font-size: 16px; font-weight: 600; color: #c9d1d9;
      margin-bottom: 16px; padding-bottom: 8px;
      border-bottom: 1px solid #21262d;
    }
    .refresh-note { font-size: 11px; color: #484f58; text-align: right; margin-top: 8px; }
    .history-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-top: 16px; }
    .history-cell {
      background: #161b22; border: 1px solid #30363d; border-radius: 6px;
      padding: 12px; text-align: center;
    }
    .history-cell .h-date { font-size: 11px; color: #8b949e; margin-bottom: 4px; }
    .history-cell .h-pnl  { font-size: 18px; font-weight: 700; }
    footer { margin-top: 40px; text-align: center; color: #484f58; font-size: 11px; border-top: 1px solid #21262d; padding: 20px; }
  </style>
</head>
<body>
<header>
  <h1>📈 NiftyBot Dashboard</h1>
  <div>
    <span class="mode">{{ mode | upper }} MODE</span>
  </div>
</header>

<div class="container">

  <!-- Summary Cards -->
  <div class="cards">
    <div class="card">
      <div class="label">Today's P&L</div>
      <div class="value {% if summary.total_pnl >= 0 %}green{% else %}red{% endif %}">
        ₹{{ "%.0f" | format(summary.total_pnl) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Trades Today</div>
      <div class="value blue">{{ summary.count }}</div>
    </div>
    <div class="card">
      <div class="label">Wins / Losses</div>
      <div class="value">
        <span class="green">{{ summary.wins }}</span>
        /
        <span class="red">{{ summary.losses }}</span>
      </div>
    </div>
    <div class="card">
      <div class="label">Daily Target</div>
      <div class="value {% if summary.target_hit %}green{% else %}grey{% endif %}">
        ₹{{ "%.0f" | format(target) }}
        {% if summary.target_hit %} ✓{% endif %}
      </div>
    </div>
    <div class="card">
      <div class="label">Max Loss Cap</div>
      <div class="value {% if summary.limit_hit %}red{% else %}grey{% endif %}">
        ₹{{ "%.0f" | format(max_loss) }}
        {% if summary.limit_hit %} ✗{% endif %}
      </div>
    </div>
    <div class="card">
      <div class="label">Open Position</div>
      <div class="value {% if summary.open > 0 %}blue{% else %}grey{% endif %}">
        {% if summary.open > 0 %}ACTIVE{% else %}FLAT{% endif %}
      </div>
    </div>
  </div>

  <!-- Today's Trades Table -->
  <div class="section-title">📋 Today's Trades — {{ today }}</div>

  {% if summary.trades %}
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Symbol</th>
        <th>Type</th>
        <th>Entry</th>
        <th>Entry ₹</th>
        <th>Exit ₹</th>
        <th>P&L pts</th>
        <th>P&L ₹</th>
        <th>Exit Reason</th>
        <th>Conf.</th>
      </tr>
    </thead>
    <tbody>
      {% for t in summary.trades %}
      <tr>
        <td class="grey">{{ loop.index }}</td>
        <td>{{ t.symbol }}</td>
        <td>
          <span class="badge {% if t.option_type == 'CE' %}badge-green{% else %}badge-red{% endif %}">
            {{ t.option_type }}
          </span>
        </td>
        <td class="grey">{{ t.entry_time[11:16] if t.entry_time else '—' }}</td>
        <td>₹{{ "%.2f" | format(t.entry_price) }}</td>
        <td>{% if t.exit_price %}₹{{ "%.2f" | format(t.exit_price) }}{% else %}<span class="blue">OPEN</span>{% endif %}</td>
        <td class="{% if (t.pnl_points or 0) >= 0 %}green{% else %}red{% endif %}">
          {% if t.pnl_points is not none %}{{ "%+.1f" | format(t.pnl_points) }}{% else %}—{% endif %}
        </td>
        <td class="{% if (t.pnl_inr or 0) >= 0 %}green{% else %}red{% endif %}">
          {% if t.pnl_inr is not none %}₹{{ "%+.0f" | format(t.pnl_inr) }}{% else %}—{% endif %}
        </td>
        <td>
          {% if t.exit_reason %}
            <span class="badge {% if 'TARGET' in (t.exit_reason or '') %}badge-green{% elif 'STOP' in (t.exit_reason or '') %}badge-red{% else %}badge-grey{% endif %}">
              {{ t.exit_reason }}
            </span>
          {% else %}
            <span class="badge badge-blue">OPEN</span>
          {% endif %}
        </td>
        <td class="grey">{{ t.confidence or '—' }}/4</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color:#484f58; padding: 24px 0;">No trades recorded today.</p>
  {% endif %}

  <div class="refresh-note">Auto-refreshes every 30 seconds</div>

  <!-- P&L History -->
  <div class="section-title" style="margin-top: 40px;">📅 Last 10 Days P&L</div>
  <div class="history-grid">
    {% for h in history %}
    <div class="history-cell">
      <div class="h-date">{{ h.date }}</div>
      <div class="h-pnl {% if h.total_pnl >= 0 %}green{% else %}red{% endif %}">
        ₹{{ "%.0f" | format(h.total_pnl) }}
      </div>
      <div style="font-size:11px; color:#8b949e; margin-top: 4px;">{{ h.count }} trades</div>
    </div>
    {% endfor %}
  </div>

</div>

<footer>NiftyBot · {{ mode | upper }} MODE · {{ today }} · Angel One SmartAPI</footer>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    summary = get_daily_summary()
    history = []
    for i in range(10):
        d = (date.today() - timedelta(days=i)).isoformat()
        history.append(get_daily_summary(d))

    return render_template_string(
        _HTML,
        summary=summary,
        history=history,
        today=date.today().isoformat(),
        mode=config.TRADING_MODE,
        target=config.DAILY_PROFIT_TARGET,
        max_loss=config.DAILY_MAX_LOSS,
    )


@app.route("/trades")
def trades_json():
    summary = get_daily_summary()
    return jsonify(summary["trades"])


@app.route("/summary")
def summary_json():
    summary = get_daily_summary()
    return jsonify(summary)


@app.route("/history")
def history_json():
    history = []
    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        history.append(get_daily_summary(d))
    return jsonify(history)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "mode": config.TRADING_MODE})


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"🌐 Dashboard starting on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=False,
        use_reloader=False,
    )
