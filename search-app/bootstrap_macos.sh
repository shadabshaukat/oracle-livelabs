#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# shellcheck source=deploy/versions.env
. ./deploy/versions.env
unset UV_PYTHON_DOWNLOADS

if [ "$(uname -s)" != "Darwin" ]; then
  echo "bootstrap_macos.sh supports macOS only." >&2
  exit 1
fi
MACOS_MAJOR=$(sw_vers -productVersion | awk -F. '{print $1}')
if [ "$MACOS_MAJOR" -lt 14 ]; then
  echo "Ollama and the locked image stack require macOS 14 Sonoma or newer." >&2
  exit 1
fi

case "$(uname -m)" in
  arm64)
    UV_TARGET=aarch64-apple-darwin
    UV_SHA256=$UV_MACOS_ARM64_SHA256
    ;;
  x86_64)
    echo "This locked full build currently supports Apple Silicon macOS only." >&2
    echo "Intel macOS requires a separate Python/PyTorch lock because the pinned PyTorch build has no Intel wheel." >&2
    exit 1
    ;;
  *)
    echo "Unsupported macOS architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

for command_name in curl shasum tar find awk sed grep install mkdir mktemp nohup sw_vers tr mv cp ps chmod sleep; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required macOS command: $command_name" >&2
    exit 1
  fi
done
if [ ! -x /usr/sbin/lsof ]; then
  echo "Missing required macOS listener inspector: /usr/sbin/lsof" >&2
  exit 1
fi

RUNTIME_ROOT="$PWD/storage/runtime/macos"
UV_HOME="$RUNTIME_ROOT/uv/$UV_VERSION"
UV_BIN="$UV_HOME/uv"
OLLAMA_HOME="$RUNTIME_ROOT/ollama/$OLLAMA_VERSION"
OLLAMA_BIN="$OLLAMA_HOME/ollama"
OLLAMA_MODELS_DIR="$RUNTIME_ROOT/models"
OLLAMA_PID_FILE="$RUNTIME_ROOT/ollama.pid"
OLLAMA_LOG_FILE="$PWD/storage/logs/ollama-macos.log"
mkdir -p "$RUNTIME_ROOT" "$PWD/storage/logs" "$OLLAMA_MODELS_DIR"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
if [ "${CLEAN_BUILD:-0}" = "1" ]; then
  rm -rf -- .venv
fi

download_verified() {
  local url=$1
  local destination=$2
  local expected_sha=$3
  local actual_sha
  curl --proto '=https' --tlsv1.2 --fail --location --retry 5 \
    --output "$destination" "$url"
  actual_sha=$(shasum -a 256 "$destination" | awk '{print $1}')
  if [ "$actual_sha" != "$expected_sha" ]; then
    echo "Checksum mismatch for $url" >&2
    echo "Expected: $expected_sha" >&2
    echo "Actual:   $actual_sha" >&2
    exit 1
  fi
}

if [ ! -x "$UV_BIN" ] || \
   [ "$("$UV_BIN" --version 2>/dev/null | awk '{print $1 " " $2}' || true)" != "uv $UV_VERSION" ]; then
  UV_ARCHIVE="$TMP_DIR/uv.tar.gz"
  download_verified \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TARGET.tar.gz" \
    "$UV_ARCHIVE" \
    "$UV_SHA256"
  mkdir -p "$TMP_DIR/uv"
  tar -xzf "$UV_ARCHIVE" -C "$TMP_DIR/uv"
  UV_SOURCE=$(find "$TMP_DIR/uv" -type f -name uv -print -quit)
  UVX_SOURCE=$(find "$TMP_DIR/uv" -type f -name uvx -print -quit)
  if [ -z "$UV_SOURCE" ] || [ -z "$UVX_SOURCE" ]; then
    echo "Pinned uv archive did not contain uv and uvx." >&2
    exit 1
  fi
  rm -rf -- "$UV_HOME"
  mkdir -p "$UV_HOME"
  install -m 0755 "$UV_SOURCE" "$UV_HOME/uv"
  install -m 0755 "$UVX_SOURCE" "$UV_HOME/uvx"
else
  echo "Pinned uv already installed in the project runtime; skipping download."
fi
if [ "$("$UV_BIN" --version | awk '{print $1 " " $2}')" != "uv $UV_VERSION" ]; then
  echo "uv version verification failed." >&2
  exit 1
fi

if ! UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_DOWNLOADS=never \
    "$UV_BIN" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
  UV_PYTHON_PREFERENCE=only-managed "$UV_BIN" python install "$PYTHON_VERSION"
else
  echo "Managed Python $PYTHON_VERSION already installed; skipping download."
fi
PYTHON_BIN=$(UV_PYTHON_PREFERENCE=only-managed "$UV_BIN" python find "$PYTHON_VERSION")
if [ "$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')" != "$PYTHON_VERSION" ]; then
  echo "Managed Python version verification failed." >&2
  exit 1
fi

ollama_cli_version() {
  if [ ! -x "$OLLAMA_BIN" ]; then
    return 1
  fi
  "$OLLAMA_BIN" --version 2>&1 \
    | awk '/version is/ || /client version is/ {print $NF}' \
    | tail -n 1
}

OLLAMA_ACTUAL_VERSION=$(ollama_cli_version || true)
if [ "${FORCE_OLLAMA_REINSTALL:-0}" = "1" ] || [ "$OLLAMA_ACTUAL_VERSION" != "$OLLAMA_VERSION" ]; then
  OLLAMA_ARCHIVE="$TMP_DIR/ollama-darwin.tgz"
  download_verified \
    "https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-darwin.tgz" \
    "$OLLAMA_ARCHIVE" \
    "$OLLAMA_MACOS_UNIVERSAL_SHA256"
  mkdir -p "$TMP_DIR/ollama"
  tar -xzf "$OLLAMA_ARCHIVE" -C "$TMP_DIR/ollama"
  if [ ! -x "$TMP_DIR/ollama/ollama" ] || [ ! -x "$TMP_DIR/ollama/llama-server" ]; then
    echo "Pinned Ollama archive had an unexpected layout." >&2
    exit 1
  fi
  ARCHIVE_VERSION=$(
    "$TMP_DIR/ollama/ollama" --version 2>&1 \
      | awk '/version is/ || /client version is/ {print $NF}' \
      | tail -n 1
  )
  if [ "$ARCHIVE_VERSION" != "$OLLAMA_VERSION" ]; then
    echo "Ollama archive version verification failed." >&2
    exit 1
  fi
  rm -rf -- "$OLLAMA_HOME"
  mkdir -p "$(dirname "$OLLAMA_HOME")"
  mv "$TMP_DIR/ollama" "$OLLAMA_HOME"
else
  echo "Pinned Ollama $OLLAMA_VERSION already installed in the project runtime; skipping download."
fi

api_version() {
  curl -fsS http://127.0.0.1:11434/api/version 2>/dev/null \
    | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))' 2>/dev/null
}

owned_ollama_pid() {
  local pid command_line
  [ -f "$OLLAMA_PID_FILE" ] || return 1
  pid=$(sed -n '1p' "$OLLAMA_PID_FILE" 2>/dev/null || true)
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  command_line=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$command_line" in
    *"$RUNTIME_ROOT/ollama/"*" serve"*) printf '%s\n' "$pid" ;;
    *) return 1 ;;
  esac
}

stop_owned_ollama() {
  local pid
  pid=$(owned_ollama_pid || true)
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  rm -f -- "$OLLAMA_PID_FILE"
}

API_VERSION=$(api_version || true)
if [ -n "$API_VERSION" ] && [ "$API_VERSION" != "$OLLAMA_VERSION" ]; then
  if owned_ollama_pid >/dev/null 2>&1; then
    stop_owned_ollama
    API_VERSION=""
  else
    echo "Port 11434 is occupied by Ollama $API_VERSION, but this build pins $OLLAMA_VERSION." >&2
    echo "Quit the other Ollama instance and rerun ./run.sh; it will not be killed automatically." >&2
    exit 1
  fi
fi

if [ -z "$API_VERSION" ]; then
  stop_owned_ollama
  echo "Starting the pinned project-local Ollama service..."
  env \
    HOME="$HOME" \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_CONTEXT_LENGTH="$OLLAMA_NUM_CTX" \
    OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE" \
    nohup "$OLLAMA_BIN" serve >> "$OLLAMA_LOG_FILE" 2>&1 < /dev/null &
  printf '%s\n' "$!" > "$OLLAMA_PID_FILE"
  WAIT_COUNT=0
  while [ "$WAIT_COUNT" -lt 60 ]; do
    API_VERSION=$(api_version || true)
    [ "$API_VERSION" = "$OLLAMA_VERSION" ] && break
    sleep 1
    WAIT_COUNT=$((WAIT_COUNT + 1))
  done
fi
if [ "$(api_version || true)" != "$OLLAMA_VERSION" ]; then
  echo "Ollama API version verification failed. See $OLLAMA_LOG_FILE" >&2
  exit 1
fi

LISTENERS=$(/usr/sbin/lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null || true)
ADDRESSES=$(printf '%s\n' "$LISTENERS" | awk 'NR > 1 {print $9}')
if [ -z "$ADDRESSES" ] || printf '%s\n' "$ADDRESSES" | grep -Ev '^(127\.0\.0\.1|\[::1\]):11434$' >/dev/null; then
  echo "Ollama must listen only on loopback port 11434; no firewall exception is allowed." >&2
  exit 1
fi

if ! "$PYTHON_BIN" scripts/verify_ollama.py \
    --base-url http://127.0.0.1:11434 >/dev/null 2>&1; then
  REGISTRY_DIGEST=$(
    curl --proto '=https' --tlsv1.2 -fsSI \
      -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      "https://registry.ollama.ai/v2/ibm/granite4/manifests/1b-q4_K_M" \
      | tr -d '\r' \
      | awk 'tolower($1) == "ollama-content-digest:" {print $2}'
  )
  if [ "$REGISTRY_DIGEST" != "$OLLAMA_MODEL_DIGEST" ]; then
    echo "Registry model digest changed; refusing to pull mutable tag $OLLAMA_MODEL." >&2
    exit 1
  fi
  OLLAMA_HOST=http://127.0.0.1:11434 "$OLLAMA_BIN" rm "$OLLAMA_MODEL" >/dev/null 2>&1 || true
  OLLAMA_HOST=http://127.0.0.1:11434 "$OLLAMA_BIN" pull "$OLLAMA_MODEL"
else
  echo "Pinned Ollama model already installed; skipping registry lookup and pull."
fi
if ! "$PYTHON_BIN" scripts/verify_ollama.py --base-url http://127.0.0.1:11434; then
  OLLAMA_HOST=http://127.0.0.1:11434 "$OLLAMA_BIN" rm "$OLLAMA_MODEL" >/dev/null 2>&1 || true
  echo "Removed model after failed digest verification." >&2
  exit 1
fi
"$PYTHON_BIN" scripts/verify_ollama.py \
  --base-url http://127.0.0.1:11434 \
  --ensure-loaded \
  --smoke

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 0600 .env
  echo "Created .env from the safe example; configure PostgreSQL and application secrets before starting."
fi

export UV_PYTHON_PREFERENCE=only-managed
"$UV_BIN" lock --check
"$UV_BIN" sync --locked --python "$PYTHON_VERSION" --extra pdf --extra image --no-dev
export UV_PYTHON_DOWNLOADS=never
"$UV_BIN" pip check
"$UV_BIN" run --locked --no-sync --python "$PYTHON_VERSION" python -m compileall -q app scripts
"$UV_BIN" run --locked --no-sync --python "$PYTHON_VERSION" python -c \
  'from app.embeddings import get_text_embedding_dim; print("Embedding model ready:", get_text_embedding_dim())'

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Optional OCR notice: Tesseract is not installed; non-OCR search/RAG remains available."
fi
echo "macOS bootstrap complete: uv $UV_VERSION, Python $PYTHON_VERSION, Ollama $OLLAMA_VERSION, $OLLAMA_MODEL."
echo "Ollama is project-local and loopback-only on 127.0.0.1:11434; no firewall rule is required."
