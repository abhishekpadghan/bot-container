#!/bin/sh
# entrypoint.sh — Fix volume permissions then drop to botuser.
#
# Named volumes mounted at runtime are owned by root.
# This script (runs as root) re-chowns /app/data and /app/logs
# to botuser before exec-ing the actual command as botuser.
#
# Also creates /tmp/smartapi_logs so SmartAPI's internal logger
# (which hardcodes a relative "logs/" path) writes to /tmp instead.

set -e

# Re-chown mounted volumes so botuser can write logs and DB
chown -R botuser:botuser /app/data /app/logs

# SmartAPI hardcodes os.path.join("logs", date) relative to CWD.
# Pre-create it under /tmp so it never hits /app/logs as root.
mkdir -p /tmp/smartapi_logs
chown -R botuser:botuser /tmp/smartapi_logs

# Drop privileges to botuser and exec the real command
# Default CMD = python main.py  (set in Dockerfile)
exec gosu botuser "$@"
