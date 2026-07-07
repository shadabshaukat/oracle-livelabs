#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "run_macos.sh supports macOS only." >&2
  exit 1
fi
if [ "$(uname -m)" != "arm64" ]; then
  echo "This locked full build currently supports Apple Silicon macOS only." >&2
  echo "Intel macOS requires a separate Python/PyTorch lock because the pinned PyTorch build has no Intel wheel." >&2
  exit 1
fi
MACOS_MAJOR=$(sw_vers -productVersion | awk -F. '{print $1}')
if [ "$MACOS_MAJOR" -lt 14 ]; then
  echo "Ollama and the locked image stack require macOS 14 Sonoma or newer." >&2
  exit 1
fi

load_environment() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
  # shellcheck source=deploy/versions.env
  . ./deploy/versions.env
}

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 0600 .env
  echo "Created .env from the safe example; configure PostgreSQL and application secrets before starting."
fi
load_environment

# shellcheck source=scripts/storage_env.sh
. ./scripts/storage_env.sh
searchapp_prepare_storage
# shellcheck source=scripts/macos_ollama.sh
. ./scripts/macos_ollama.sh

LOCK_DIR="$SEARCHAPP_RUN_DIR/searchapp.macos.run.lock"
acquire_lock() {
  local owner=""
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    return 0
  fi
  if [ -f "$LOCK_DIR/pid" ]; then
    owner=$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)
  fi
  case "$owner" in
    ''|*[!0-9]*) ;;
    *)
      if kill -0 "$owner" 2>/dev/null; then
        echo "Another search-app run or first-use setup is already active for this checkout (PID $owner)." >&2
        exit 1
      fi
      ;;
  esac
  rm -rf -- "$LOCK_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Another search-app process acquired the startup lock." >&2
    exit 1
  fi
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
}

cleanup_lock() {
  rm -rf -- "$LOCK_DIR"
}

acquire_lock
trap cleanup_lock EXIT INT TERM

RUNTIME_ROOT="$SEARCHAPP_RUNTIME_DIR/macos"
UV_BIN="$RUNTIME_ROOT/uv/$UV_VERSION/uv"
PYTHON_BIN=""
BOOTSTRAP_RAN=0

runtime_is_ready() {
  [ -x "$UV_BIN" ] || return 1
  [ "$("$UV_BIN" --version 2>/dev/null | awk '{print $1 " " $2}')" = "uv $UV_VERSION" ] || return 1
  export UV_PYTHON_PREFERENCE=only-managed
  export UV_PYTHON_DOWNLOADS=never
  PYTHON_BIN=$("$UV_BIN" python find "$PYTHON_VERSION" 2>/dev/null) || return 1
  [ "$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())' 2>/dev/null)" = "$PYTHON_VERSION" ]
}

bootstrap_once() {
  local reason=$1
  if [ "$BOOTSTRAP_RAN" -eq 1 ]; then
    echo "macOS bootstrap completed, but the runtime is still not ready: $reason" >&2
    exit 1
  fi
  echo "Runtime setup required ($reason). Running the pinned macOS bootstrap..."
  ./bootstrap_macos.sh
  BOOTSTRAP_RAN=1
  load_environment
}

if ! runtime_is_ready; then
  bootstrap_once "uv $UV_VERSION or managed Python $PYTHON_VERSION is missing"
fi
if ! runtime_is_ready; then
  echo "Pinned macOS uv/Python verification failed after bootstrap." >&2
  exit 1
fi

LLM_PROVIDER_EFFECTIVE=${LLM_PROVIDER:-ollama}
OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL_EFFECTIVE%/}

case "$LLM_PROVIDER_EFFECTIVE" in
  [Oo][Ll][Ll][Aa][Mm][Aa])
    OLLAMA_API_VERSION=$(searchapp_macos_ollama_api_version "$OLLAMA_BASE_URL_EFFECTIVE" "$PYTHON_BIN" || true)
    case "$OLLAMA_BASE_URL_EFFECTIVE" in
      http://127.0.0.1:11434|http://localhost:11434)
        if [ -z "$OLLAMA_API_VERSION" ]; then
          bootstrap_once "no running local Ollama API was found"
          runtime_is_ready
          OLLAMA_API_VERSION=$(searchapp_macos_ollama_api_version "$OLLAMA_BASE_URL_EFFECTIVE" "$PYTHON_BIN" || true)
        fi
        [ -n "$OLLAMA_API_VERSION" ] || {
          echo "Ollama did not expose its local API after macOS setup." >&2
          exit 1
        }
        searchapp_macos_loopback_listener_is_safe || {
          echo "Existing Ollama must listen only on loopback port 11434; it was left unchanged." >&2
          exit 1
        }
        ;;
      *)
        echo "Using explicitly configured non-local Ollama endpoint: $OLLAMA_BASE_URL_EFFECTIVE"
        [ -n "$OLLAMA_API_VERSION" ] || {
          echo "Configured Ollama endpoint did not return an API version." >&2
          exit 1
        }
        ;;
    esac

    # macOS adopts the existing server version while retaining exact model
    # digest/quantization checks. Linux continues enforcing its pinned version.
    export OLLAMA_VERSION=$OLLAMA_API_VERSION
    echo "Using existing macOS Ollama version $OLLAMA_API_VERSION."
    if ! "$PYTHON_BIN" scripts/verify_ollama.py \
        --base-url "$OLLAMA_BASE_URL_EFFECTIVE" \
        --version "$OLLAMA_API_VERSION" \
        --ensure-loaded; then
      case "$OLLAMA_BASE_URL_EFFECTIVE" in
        http://127.0.0.1:11434|http://localhost:11434)
          bootstrap_once "the required Ollama model is missing or incompatible"
          runtime_is_ready
          OLLAMA_API_VERSION=$(searchapp_macos_ollama_api_version "$OLLAMA_BASE_URL_EFFECTIVE" "$PYTHON_BIN" || true)
          [ -n "$OLLAMA_API_VERSION" ] || {
            echo "Ollama API became unavailable after macOS setup." >&2
            exit 1
          }
          export OLLAMA_VERSION=$OLLAMA_API_VERSION
          "$PYTHON_BIN" scripts/verify_ollama.py \
            --base-url "$OLLAMA_BASE_URL_EFFECTIVE" \
            --version "$OLLAMA_API_VERSION" \
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
        searchapp_macos_loopback_listener_is_safe || {
          echo "Ollama must listen only on loopback port 11434; no firewall exception is permitted." >&2
          exit 1
        }
        ;;
    esac
    ;;
esac

export UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON_DOWNLOADS=never
"$UV_BIN" lock --check
if [ "${SKIP_DEPS:-}" != "1" ] && [ "${SKIP_DEPS:-}" != "true" ]; then
  "$UV_BIN" sync --locked --python "$PYTHON_VERSION" --extra pdf --extra image --no-dev
else
  echo "Skipping dependency sync (SKIP_DEPS set)."
fi

if [ "${RUN_PREPARE_ONLY:-}" = "1" ] || [ "${RUN_PREPARE_ONLY:-}" = "true" ]; then
  echo "macOS runtime preparation complete; application launch skipped (RUN_PREPARE_ONLY set)."
  exit 0
fi

# Preserve the lock directory across exec. Its PID is this shell's PID and
# therefore remains the uv process PID; the next run safely reclaims it once dead.
trap - EXIT INT TERM
exec "$UV_BIN" run --locked --no-sync --python "$PYTHON_VERSION" searchapp
