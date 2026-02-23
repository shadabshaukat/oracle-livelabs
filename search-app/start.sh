#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Load .env if present for local convenience
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

LOG_DIR="storage/logs"
PID_FILE="storage/searchapp.pid"
LOG_FILE="$LOG_DIR/searchapp.log"

mkdir -p "$LOG_DIR"

if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    echo "Search app already running (PID $PID)."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup ./run.sh > "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

echo "Search app started (PID $PID). Logs: $LOG_FILE"