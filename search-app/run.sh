#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present for local convenience
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Install dependencies including optional PDF + image extras for robust parsing/search
if [ "${SKIP_DEPS:-}" != "1" ] && [ "${SKIP_DEPS:-}" != "true" ]; then
  uv sync --extra pdf --extra image
else
  echo "Skipping dependency sync (SKIP_DEPS set)."
fi
uv run searchapp
