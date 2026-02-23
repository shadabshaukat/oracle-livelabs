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
uv sync --extra pdf --extra image
uv run searchapp
