#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present for local convenience
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

uv sync
uv run searchapp
