#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

load_environment() {
  # .env contains deployment values; versions.env contains immutable build pins
  # and intentionally wins for version/model identity.
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
  # shellcheck source=deploy/versions.env
  . ./deploy/versions.env
}

load_environment

if ! command -v flock >/dev/null 2>&1; then
  echo "Missing required host command: flock (normally provided by util-linux)." >&2
  exit 1
fi
mkdir -p storage
exec 9>storage/searchapp.run.lock
if ! flock -n 9; then
  echo "Another search-app run or first-use setup is already active for this checkout." >&2
  exit 1
fi

BOOTSTRAP_RAN=0
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
PYTHON_BIN=""

bootstrap_once() {
  local reason=$1
  if [ "$BOOTSTRAP_RAN" -eq 1 ]; then
    echo "Bootstrap completed, but the runtime is still not ready: $reason" >&2
    exit 1
  fi
  if [ "$(uname -s)" != "Linux" ]; then
    echo "Automatic first-run setup is supported on Linux only. Run on a supported Linux host or prepare the pinned runtime manually." >&2
    exit 1
  fi
  echo "Runtime setup required ($reason). Running the pinned Linux bootstrap..."
  ./bootstrap_linux.sh
  BOOTSTRAP_RAN=1
  UV_BIN=/usr/local/bin/uv
  load_environment
}

runtime_is_ready() {
  [ -n "$UV_BIN" ] && [ -x "$UV_BIN" ] || return 1
  [ "$($UV_BIN --version 2>/dev/null | awk '{print $1 " " $2}')" = "uv $UV_VERSION" ] || return 1

  export UV_PYTHON_PREFERENCE=only-managed
  export UV_PYTHON_DOWNLOADS=never
  PYTHON_BIN=$($UV_BIN python find "$PYTHON_VERSION" 2>/dev/null) || return 1
  [ "$($PYTHON_BIN -c 'import platform; print(platform.python_version())' 2>/dev/null)" = "$PYTHON_VERSION" ]
}

if ! runtime_is_ready; then
  bootstrap_once "uv $UV_VERSION or managed Python $PYTHON_VERSION is missing"
fi
if ! runtime_is_ready; then
  echo "Pinned uv/Python verification failed after bootstrap." >&2
  exit 1
fi

ollama_cli_is_ready() {
  [ -x /usr/local/bin/ollama ] && [ -d /usr/local/lib/ollama ] || return 1
  local actual_version
  actual_version=$(
    /usr/local/bin/ollama --version 2>&1 \
      | awk '/version is/ || /client version is/ {print $NF}' \
      | tail -n 1
  )
  [ "$actual_version" = "$OLLAMA_VERSION" ]
}

ollama_service_is_ready() {
  command -v systemctl >/dev/null 2>&1 || return 1
  [ -f /etc/systemd/system/ollama.service ] || return 1
  cmp -s deploy/ollama.service /etc/systemd/system/ollama.service || return 1
  systemctl is-enabled --quiet ollama.service
}

start_existing_ollama_service() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl cat ollama.service >/dev/null 2>&1 || return 1
  echo "Ollama is installed but its local API is stopped; starting the existing service..."
  if [ -t 0 ]; then
    sudo systemctl start ollama.service
  else
    sudo -n systemctl start ollama.service
  fi
  for _ in $(seq 1 30); do
    if curl -fsS "$OLLAMA_BASE_URL_EFFECTIVE/api/version" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

loopback_listener_is_safe() {
  command -v ss >/dev/null 2>&1 || return 1
  local listeners
  listeners=$(ss -ltnH 'sport = :11434')
  [ -n "$listeners" ] || return 1
  ! echo "$listeners" | awk '{print $4}' | grep -Ev '^127\.0\.0\.1:11434$' >/dev/null
}

LLM_PROVIDER_EFFECTIVE=${LLM_PROVIDER:-ollama}
OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL_EFFECTIVE%/}

if [ "${LLM_PROVIDER_EFFECTIVE,,}" = "ollama" ]; then
  case "$OLLAMA_BASE_URL_EFFECTIVE" in
    http://127.0.0.1:11434|http://localhost:11434)
      if ! ollama_cli_is_ready || ! ollama_service_is_ready; then
        bootstrap_once "pinned Ollama $OLLAMA_VERSION or its service configuration is missing"
        runtime_is_ready
      fi

      if ! curl -fsS "$OLLAMA_BASE_URL_EFFECTIVE/api/version" >/dev/null 2>&1; then
        start_existing_ollama_service || bootstrap_once "the local Ollama service is unavailable"
        runtime_is_ready
      fi
      ;;
    *)
      echo "Using explicitly configured non-local Ollama endpoint: $OLLAMA_BASE_URL_EFFECTIVE"
      ;;
  esac

  if ! "$PYTHON_BIN" scripts/verify_ollama.py \
      --base-url "$OLLAMA_BASE_URL_EFFECTIVE" \
      --ensure-loaded; then
    case "$OLLAMA_BASE_URL_EFFECTIVE" in
      http://127.0.0.1:11434|http://localhost:11434)
        bootstrap_once "the pinned Ollama model/runtime failed verification"
        runtime_is_ready
        "$PYTHON_BIN" scripts/verify_ollama.py \
          --base-url "$OLLAMA_BASE_URL_EFFECTIVE" \
          --ensure-loaded
        ;;
      *)
        echo "Configured Ollama endpoint failed verification; automatic bootstrap only repairs the local endpoint." >&2
        exit 1
        ;;
    esac
  fi

  case "$OLLAMA_BASE_URL_EFFECTIVE" in
    http://127.0.0.1:11434|http://localhost:11434)
      if ! loopback_listener_is_safe; then
        bootstrap_once "Ollama is not bound exclusively to 127.0.0.1:11434"
        runtime_is_ready
        loopback_listener_is_safe || {
          echo "Ollama listener safety verification failed after bootstrap." >&2
          exit 1
        }
      fi
      ;;
  esac
fi

export UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON_DOWNLOADS=never

"$UV_BIN" lock --check

# Install only the exact committed dependency graph, including existing PDF
# and image workflows. uv makes this a no-op when the environment is current.
if [ "${SKIP_DEPS:-}" != "1" ] && [ "${SKIP_DEPS:-}" != "true" ]; then
  "$UV_BIN" sync --locked --python "$PYTHON_VERSION" --extra pdf --extra image --no-dev
else
  echo "Skipping dependency sync (SKIP_DEPS set)."
fi

exec "$UV_BIN" run --locked --no-sync --python "$PYTHON_VERSION" searchapp
