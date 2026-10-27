"""
backup.py — copy the trade/order history out of the data volume onto your PC.

    ./run.sh backup

Writes bot-container/backups/: trades_history.csv, orders_history.csv and dated
SQLite snapshots (trades_YYYY-MM-DD.db). Safe to run any time, even while the bot runs.
"""
import os
import shutil

import paper_trader

paper_trader.export_history()                                   # refresh CSVs + today's snapshot
dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
os.makedirs(dest, exist_ok=True)
shutil.copytree(paper_trader.EXPORT_DIR, dest, dirs_exist_ok=True)
print(f"✅ Backed up to {dest}:")
for name in sorted(os.listdir(dest)):
    print("   ", name)
