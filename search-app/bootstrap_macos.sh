#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
# shellcheck source=deploy/versions.env
. ./deploy/versions.env


# shellcheck source=scripts/storage_env.sh
. ./scripts/storage_env.sh
searchapp_prepare_storage
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

# shellcheck source=scripts/macos_ollama.sh
. ./scripts/macos_ollama.sh

RUNTIME_ROOT="$SEARCHAPP_RUNTIME_DIR/macos"
UV_HOME="$RUNTIME_ROOT/uv/$UV_VERSION"
UV_BIN="$UV_HOME/uv"
PINNED_OLLAMA_VERSION=$OLLAMA_VERSION
MANAGED_OLLAMA_HOME="$RUNTIME_ROOT/ollama/$PINNED_OLLAMA_VERSION"
MANAGED_OLLAMA_BIN="$MANAGED_OLLAMA_HOME/ollama"
OLLAMA_MODELS_DIR="$RUNTIME_ROOT/models"
OLLAMA_PID_FILE="$RUNTIME_ROOT/ollama.pid"
OLLAMA_LOG_FILE="$SEARCHAPP_LOG_DIR/ollama-macos.log"
mkdir -p "$RUNTIME_ROOT" "$OLLAMA_MODELS_DIR"

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

install_managed_ollama() {
  OLLAMA_ARCHIVE="$TMP_DIR/ollama-darwin.tgz"
  download_verified \
    "https://github.com/ollama/ollama/releases/download/v$PINNED_OLLAMA_VERSION/ollama-darwin.tgz" \
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
  if [ "$ARCHIVE_VERSION" != "$PINNED_OLLAMA_VERSION" ]; then
    echo "Ollama archive version verification failed." >&2
    exit 1
  fi
  rm -rf -- "$MANAGED_OLLAMA_HOME"
  mkdir -p "$(dirname "$MANAGED_OLLAMA_HOME")"
  mv "$TMP_DIR/ollama" "$MANAGED_OLLAMA_HOME"
  echo "Installed self-contained Ollama $PINNED_OLLAMA_VERSION under the application home; Homebrew was not required."
}

OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
OLLAMA_BASE_URL_EFFECTIVE=${OLLAMA_BASE_URL_EFFECTIVE%/}

API_VERSION=$(searchapp_macos_ollama_api_version "$OLLAMA_BASE_URL_EFFECTIVE" "$PYTHON_BIN" || true)
SELECTED_OLLAMA_BIN=""
OLLAMA_SOURCE="running local API"

if [ -n "$API_VERSION" ]; then
  searchapp_macos_loopback_listener_is_safe || {
    echo "Existing Ollama must listen only on loopback port 11434; it was left unchanged." >&2
    exit 1
  }
  echo "Reusing the existing macOS Ollama API (version $API_VERSION); no installation change was made."
else
  if [ -n "${OLLAMA_CLI_PATH:-}" ] && [ ! -x "$OLLAMA_CLI_PATH" ]; then
    echo "OLLAMA_CLI_PATH is not executable: $OLLAMA_CLI_PATH" >&2
    exit 1
  fi
  SELECTED_OLLAMA_BIN=$(searchapp_macos_find_ollama_cli "$MANAGED_OLLAMA_BIN" || true)
  if [ -z "$SELECTED_OLLAMA_BIN" ]; then
    echo "No existing macOS Ollama installation was found; installing the pinned self-contained fallback."
    install_managed_ollama
    SELECTED_OLLAMA_BIN=$MANAGED_OLLAMA_BIN
    OLLAMA_SOURCE="managed fallback"
  else
    OLLAMA_SOURCE="existing installation"
    SELECTED_CLI_VERSION=$(searchapp_macos_ollama_cli_version "$SELECTED_OLLAMA_BIN" || true)
    echo "Reusing existing macOS Ollama CLI: $SELECTED_OLLAMA_BIN${SELECTED_CLI_VERSION:+ (version $SELECTED_CLI_VERSION)}"
    if [ "${FORCE_OLLAMA_REINSTALL:-0}" = "1" ] && [ "$SELECTED_OLLAMA_BIN" = "$MANAGED_OLLAMA_BIN" ]; then
      echo "Refreshing only the app-managed fallback because FORCE_OLLAMA_REINSTALL=1."
      install_managed_ollama
    elif [ "${FORCE_OLLAMA_REINSTALL:-0}" = "1" ]; then
      echo "FORCE_OLLAMA_REINSTALL does not replace external macOS Ollama; leaving it unchanged."
    fi
  fi

  searchapp_macos_start_ollama \
    "$SELECTED_OLLAMA_BIN" \
    "$MANAGED_OLLAMA_BIN" \
    "$OLLAMA_MODELS_DIR" \
    "$OLLAMA_PID_FILE" \
    "$OLLAMA_LOG_FILE"

  WAIT_COUNT=0
  while [ "$WAIT_COUNT" -lt 60 ]; do
    API_VERSION=$(searchapp_macos_ollama_api_version "$OLLAMA_BASE_URL_EFFECTIVE" "$PYTHON_BIN" || true)
    [ -n "$API_VERSION" ] && break
    sleep 1
    WAIT_COUNT=$((WAIT_COUNT + 1))
  done
fi

if [ -z "$API_VERSION" ]; then
  echo "Existing Ollama could not start its local API. It was not replaced; see $OLLAMA_LOG_FILE" >&2
  exit 1
fi

searchapp_macos_loopback_listener_is_safe || {
  echo "Ollama must listen only on loopback port 11434; the existing installation was left unchanged." >&2
  exit 1
}

# The fallback installer remains pinned, but an existing macOS Ollama version
# is accepted as-is when its stable local APIs and the exact model work.
export OLLAMA_VERSION=$API_VERSION
if ! "$PYTHON_BIN" scripts/verify_ollama.py \
    --base-url "$OLLAMA_BASE_URL_EFFECTIVE" \
    --version "$API_VERSION" \
    --pull-if-missing \
    --ensure-loaded \
    --smoke; then
  echo "The existing Ollama version $API_VERSION is incompatible with the required model/API workflow." >&2
  echo "It was left unchanged. Upgrade it manually or set OLLAMA_CLI_PATH to a compatible installation." >&2
  exit 1
fi

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
echo "macOS bootstrap complete: uv $UV_VERSION, Python $PYTHON_VERSION, Ollama $API_VERSION ($OLLAMA_SOURCE), $OLLAMA_MODEL."
echo "Ollama is loopback-only on 127.0.0.1:11434; no Homebrew dependency or firewall rule is required."
