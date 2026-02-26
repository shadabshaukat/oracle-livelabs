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
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/ready}"

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

if command -v curl >/dev/null 2>&1; then
  echo "Waiting for health check: $HEALTH_URL"
  for _ in {1..20}; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      echo "Search app is healthy."
      exit 0
    fi
    sleep 0.5
  done
  echo "Warning: health check did not pass yet. Check logs: $LOG_FILE"
else
  echo "curl not found; skipping health check."
fi