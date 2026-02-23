#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="storage/searchapp.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "No PID file found. Is the server running?"
  exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [ -z "${PID:-}" ]; then
  echo "PID file was empty."
  rm -f "$PID_FILE"
  exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped search app (PID $PID)."
else
  echo "Process $PID not running. Removing stale PID file."
fi

rm -f "$PID_FILE"