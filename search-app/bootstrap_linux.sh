#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# shellcheck source=deploy/versions.env
. ./deploy/versions.env

# A parent run.sh intentionally disables implicit interpreter downloads after
# verification. Bootstrap is the one controlled place where the pinned Python
# may be downloaded, so do not inherit that guard into first-use installation.
unset UV_PYTHON_DOWNLOADS

if [ "$(uname -s)" != "Linux" ]; then
  echo "bootstrap_linux.sh supports Linux hosts only." >&2
  exit 1
fi
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "Run this script as the application owner, not root; it uses sudo only for system files." >&2
  exit 1
fi

for command_name in curl sha256sum tar find sudo systemctl ss awk sed grep cmp getent groupadd useradd install cp seq tr mktemp flock; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required host command: $command_name" >&2
    exit 1
  fi
done

case "$(uname -m)" in
  x86_64)
    UV_TARGET=x86_64-unknown-linux-gnu
    UV_SHA256=$UV_LINUX_AMD64_SHA256
    OLLAMA_TARGET=amd64
    OLLAMA_SHA256=$OLLAMA_LINUX_AMD64_SHA256
    ;;
  aarch64|arm64)
    UV_TARGET=aarch64-unknown-linux-gnu
    UV_SHA256=$UV_LINUX_ARM64_SHA256
    OLLAMA_TARGET=arm64
    OLLAMA_SHA256=$OLLAMA_LINUX_ARM64_SHA256
    ;;
  *)
    echo "Unsupported Linux architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

sudo -v
if [ "${CLEAN_BUILD:-0}" = "1" ]; then
  rm -rf -- .venv
fi
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

download_verified() {
  local url=$1
  local destination=$2
  local expected_sha=$3
  curl --proto '=https' --tlsv1.2 --fail --location --retry 5 --retry-all-errors \
    --output "$destination" "$url"
  printf '%s  %s\n' "$expected_sha" "$destination" | sha256sum --check --status
}

if [ ! -x /usr/local/bin/uv ] || \
   [ "$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $1 " " $2}' || true)" != "uv $UV_VERSION" ]; then
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
    echo "Pinned uv archive did not contain the expected uv/uvx binaries." >&2
    exit 1
  fi
  sudo install -m 0755 "$UV_SOURCE" /usr/local/bin/uv
  sudo install -m 0755 "$UVX_SOURCE" /usr/local/bin/uvx
else
  echo "Pinned uv already installed; skipping download."
fi
UV_ACTUAL_VERSION=$(/usr/local/bin/uv --version | awk '{print $1 " " $2}')
if [ "$UV_ACTUAL_VERSION" != "uv $UV_VERSION" ]; then
  echo "uv version verification failed." >&2
  exit 1
fi

if ! UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_DOWNLOADS=never \
    /usr/local/bin/uv python find "$PYTHON_VERSION" >/dev/null 2>&1; then
  UV_PYTHON_PREFERENCE=only-managed /usr/local/bin/uv python install "$PYTHON_VERSION"
else
  echo "Managed Python $PYTHON_VERSION already installed; skipping download."
fi
PYTHON_BIN=$(UV_PYTHON_PREFERENCE=only-managed /usr/local/bin/uv python find "$PYTHON_VERSION")
if [ "$($PYTHON_BIN -c 'import platform; print(platform.python_version())')" != "$PYTHON_VERSION" ]; then
  echo "Managed Python version verification failed." >&2
  exit 1
fi

OLLAMA_ACTUAL_VERSION=$(
  if [ -x /usr/local/bin/ollama ]; then
    /usr/local/bin/ollama --version 2>&1 \
      | awk '/version is/ || /client version is/ {print $NF}' \
      | tail -n 1
  fi
)
OLLAMA_BINARY_CHANGED=0
if [ "${FORCE_OLLAMA_REINSTALL:-0}" = "1" ] || \
   [ ! -x /usr/local/bin/ollama ] || \
   [ ! -d /usr/local/lib/ollama ] || \
   [ "$OLLAMA_ACTUAL_VERSION" != "$OLLAMA_VERSION" ]; then
  OLLAMA_ARCHIVE="$TMP_DIR/ollama.tar.zst"
  download_verified \
    "https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-linux-$OLLAMA_TARGET.tar.zst" \
    "$OLLAMA_ARCHIVE" \
    "$OLLAMA_SHA256"
  mkdir -p "$TMP_DIR/ollama"
  "$PYTHON_BIN" scripts/extract_tar_zst.py "$OLLAMA_ARCHIVE" "$TMP_DIR/ollama"
  if [ ! -x "$TMP_DIR/ollama/bin/ollama" ] || [ ! -d "$TMP_DIR/ollama/lib/ollama" ]; then
    echo "Pinned Ollama archive had an unexpected layout." >&2
    exit 1
  fi
  sudo systemctl stop ollama.service 2>/dev/null || true
  sudo install -m 0755 "$TMP_DIR/ollama/bin/ollama" /usr/local/bin/ollama
  sudo rm -rf /usr/local/lib/ollama
  sudo install -d -m 0755 /usr/local/lib
  sudo cp -a "$TMP_DIR/ollama/lib/ollama" /usr/local/lib/ollama
  OLLAMA_BINARY_CHANGED=1
else
  echo "Pinned Ollama $OLLAMA_VERSION already installed; skipping download."
fi

if ! getent group ollama >/dev/null 2>&1; then
  sudo groupadd --system ollama
fi
if ! id ollama >/dev/null 2>&1; then
  NOLOGIN=$(command -v nologin || printf '/sbin/nologin')
  sudo useradd --system --gid ollama --home-dir /var/lib/ollama --create-home --shell "$NOLOGIN" ollama
fi
sudo install -d -o ollama -g ollama -m 0750 /var/lib/ollama /var/lib/ollama/models
OLLAMA_SERVICE_CHANGED=0
if [ ! -f /etc/systemd/system/ollama.service ] || \
   ! cmp -s deploy/ollama.service /etc/systemd/system/ollama.service; then
  sudo install -m 0644 deploy/ollama.service /etc/systemd/system/ollama.service
  sudo systemctl daemon-reload
  OLLAMA_SERVICE_CHANGED=1
fi
sudo systemctl enable ollama.service
if [ "$OLLAMA_BINARY_CHANGED" -eq 1 ] || [ "$OLLAMA_SERVICE_CHANGED" -eq 1 ]; then
  sudo systemctl restart ollama.service
elif ! systemctl is-active --quiet ollama.service; then
  sudo systemctl start ollama.service
else
  echo "Ollama service is already configured and running; leaving it undisturbed."
fi

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:11434/api/version >/dev/null
OLLAMA_ACTUAL_VERSION=$(
  /usr/local/bin/ollama --version 2>&1 \
    | awk '/version is/ || /client version is/ {print $NF}' \
    | tail -n 1
)
if [ "$OLLAMA_ACTUAL_VERSION" != "$OLLAMA_VERSION" ]; then
  echo "Ollama CLI version verification failed." >&2
  exit 1
fi
OLLAMA_API_VERSION=$(
  curl -fsS http://127.0.0.1:11434/api/version \
    | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))'
)
if [ "$OLLAMA_API_VERSION" != "$OLLAMA_VERSION" ]; then
  echo "Ollama server version $OLLAMA_API_VERSION does not match $OLLAMA_VERSION; restarting the pinned service."
  sudo systemctl restart ollama.service
  for _ in $(seq 1 30); do
    if [ "$(curl -fsS http://127.0.0.1:11434/api/version 2>/dev/null \
        | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))' 2>/dev/null || true)" = "$OLLAMA_VERSION" ]; then
      break
    fi
    sleep 1
  done
  OLLAMA_API_VERSION=$(
    curl -fsS http://127.0.0.1:11434/api/version \
      | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))'
  )
  if [ "$OLLAMA_API_VERSION" != "$OLLAMA_VERSION" ]; then
    echo "Ollama server version verification failed." >&2
    exit 1
  fi
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
  OLLAMA_HOST=http://127.0.0.1:11434 /usr/local/bin/ollama rm "$OLLAMA_MODEL" >/dev/null 2>&1 || true
  OLLAMA_HOST=http://127.0.0.1:11434 /usr/local/bin/ollama pull "$OLLAMA_MODEL"
else
  echo "Pinned Ollama model already installed; skipping registry lookup and pull."
fi
if ! "$PYTHON_BIN" scripts/verify_ollama.py --base-url http://127.0.0.1:11434; then
  OLLAMA_HOST=http://127.0.0.1:11434 /usr/local/bin/ollama rm "$OLLAMA_MODEL" 2>/dev/null || true
  echo "Removed model after failed digest verification." >&2
  exit 1
fi

LISTENERS=$(ss -ltnH 'sport = :11434')
if [ -z "$LISTENERS" ] || echo "$LISTENERS" | awk '{print $4}' | grep -Ev '^127\.0\.0\.1:11434$' >/dev/null; then
  echo "Ollama must listen only on 127.0.0.1:11434; no public firewall rule is allowed." >&2
  exit 1
fi

"$PYTHON_BIN" scripts/verify_ollama.py \
  --base-url http://127.0.0.1:11434 \
  --ensure-loaded \
  --smoke

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 0600 .env
  echo "Created .env from the safe example; configure database and application secrets before starting."
fi

export UV_PYTHON_PREFERENCE=only-managed
/usr/local/bin/uv lock --check
/usr/local/bin/uv sync --locked --python "$PYTHON_VERSION" --extra pdf --extra image --no-dev
export UV_PYTHON_DOWNLOADS=never
/usr/local/bin/uv pip check
/usr/local/bin/uv run --locked --no-sync --python "$PYTHON_VERSION" python -m compileall -q app scripts
/usr/local/bin/uv run --locked --no-sync --python "$PYTHON_VERSION" python -c \
  'from app.embeddings import get_text_embedding_dim; print("Embedding model ready:", get_text_embedding_dim())'

echo "Bootstrap complete: uv $UV_VERSION, Python $PYTHON_VERSION, Ollama $OLLAMA_VERSION, $OLLAMA_MODEL."
echo "Ollama is loopback-only on 127.0.0.1:11434; do not add an ingress/firewall rule for that port."
echo "When invoked by run.sh, startup now continues automatically; otherwise start with ./start.sh or ./run.sh."
