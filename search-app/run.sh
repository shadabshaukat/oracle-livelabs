#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present for local convenience
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Install dependencies including optional PDF extras for robust PDF parsing
uv sync --extra pdf
uv run searchapp
