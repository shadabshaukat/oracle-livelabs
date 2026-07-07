#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# shellcheck source=scripts/storage_env.sh
. ./scripts/storage_env.sh
searchapp_prepare_storage

PID_FILE="$SEARCHAPP_RUN_DIR/searchapp.pid"

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
  echo "Stopping search app (PID $PID)..."
  for _ in {1..20}; do
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "Stopped search app (PID $PID)."
      rm -f "$PID_FILE"
      exit 0
    fi
    sleep 0.5
  done
  echo "Process $PID did not exit in time. Sending SIGKILL."
  kill -9 "$PID" 2>/dev/null || true
else
  echo "Process $PID not running. Removing stale PID file."
fi

rm -f "$PID_FILE"
