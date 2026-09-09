"""
report.py — CLI P&L report generator.
Run via: python report.py  OR  ./run.sh report
"""

from datetime import date, timedelta
from paper_trader import get_daily_summary
import config


def print_report(trade_date: str = None) -> None:
    s = get_daily_summary(trade_date)

    print()
    print("=" * 62)
    print(f"  📈 NiftyBot Daily Report — {s['date']}  [{config.TRADING_MODE.upper()} MODE]")
    print("=" * 62)
    print(f"  Total P&L   : ₹{s['total_pnl']:+.0f}")
    print(f"  Trades      : {s['count']}  (W: {s['wins']}  L: {s['losses']}  Open: {s['open']})")
    print(f"  Target      : ₹{config.DAILY_PROFIT_TARGET:.0f}  {'✅ HIT' if s['target_hit'] else '⏳ pending'}")
    print(f"  Loss limit  : ₹{config.DAILY_MAX_LOSS:.0f}  {'🛑 HIT' if s['limit_hit'] else '✅ safe'}")
    print("-" * 62)

    if s["trades"]:
        print(f"  {'#':<3} {'Symbol':<22} {'OT':<4} {'Entry':>7} {'Exit':>7} {'Pts':>6} {'₹ P&L':>8}  Reason")
        print("  " + "-" * 58)
        for i, t in enumerate(s["trades"], 1):
            pts   = f"{t['pnl_points']:+.1f}" if t.get("pnl_points") is not None else "OPEN"
            inr   = f"₹{t['pnl_inr']:+.0f}" if t.get("pnl_inr") is not None else "—"
            entry = f"₹{t['entry_price']:.2f}"
            ex    = f"₹{t['exit_price']:.2f}" if t.get("exit_price") else "OPEN"
            print(f"  {i:<3} {t['symbol']:<22} {t['option_type']:<4} {entry:>7} {ex:>7} {pts:>6} {inr:>8}  {t.get('exit_reason','—')}")
    else:
        print("  No trades recorded for this date.")

    print("=" * 62)
    print()


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    print_report(d)

    # Also print last 7 days summary
    print("  📅 Last 7 Days:")
    print(f"  {'Date':<12} {'P&L':>8}  {'Trades':>7}  {'W/L'}")
    print("  " + "-" * 38)
    total = 0.0
    for i in range(7):
        day = (date.today() - timedelta(days=i)).isoformat()
        s   = get_daily_summary(day)
        bar = "▓" * min(int(abs(s["total_pnl"]) / 100), 15)
        sign = "+" if s["total_pnl"] >= 0 else ""
        total += s["total_pnl"]
        print(f"  {day:<12} {sign}₹{s['total_pnl']:>6.0f}  {s['count']:>6}  {s['wins']}W/{s['losses']}L  {bar}")
    print("  " + "-" * 38)
    print(f"  {'7-day total':<12} ₹{total:>+.0f}")
    print()
