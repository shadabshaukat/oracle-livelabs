#!/usr/bin/env bash

# macOS-only Ollama discovery and lifecycle helpers. Keep this compatible with
# the Bash 3.2 included with macOS. Linux intentionally uses its existing
# pinned systemd implementation instead.

searchapp_macos_ollama_api_version() {
  local base_url=$1
  local python_bin=$2
  curl -fsS "${base_url%/}/api/version" 2>/dev/null \
    | "$python_bin" -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))' 2>/dev/null
}

searchapp_macos_find_ollama_cli() {
  local managed_bin=$1
  local path_bin=""
  local candidate=""

  if [ -n "${OLLAMA_CLI_PATH:-}" ]; then
    if [ -x "$OLLAMA_CLI_PATH" ]; then
      printf '%s\n' "$OLLAMA_CLI_PATH"
      return 0
    fi
    echo "OLLAMA_CLI_PATH is configured but is not executable: $OLLAMA_CLI_PATH" >&2
    return 1
  fi

  # Prefer the official macOS app over a potentially stale Homebrew symlink.
  for candidate in \
    /Applications/Ollama.app/Contents/Resources/ollama \
    "$HOME/Applications/Ollama.app/Contents/Resources/ollama"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  path_bin=$(command -v ollama 2>/dev/null || true)
  if [ -n "$path_bin" ] && [ -x "$path_bin" ]; then
    printf '%s\n' "$path_bin"
    return 0
  fi

  for candidate in /opt/homebrew/bin/ollama /usr/local/bin/ollama "$managed_bin"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

searchapp_macos_ollama_cli_version() {
  local ollama_bin=$1
  "$ollama_bin" --version 2>&1 \
    | awk '/version is/ || /client version is/ {print $NF}' \
    | tail -n 1
}

searchapp_macos_loopback_listener_is_safe() {
  local listeners addresses
  listeners=$(/usr/sbin/lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null || true)
  [ -n "$listeners" ] || return 1
  addresses=$(printf '%s\n' "$listeners" | awk 'NR > 1 {print $9}')
  [ -n "$addresses" ] || return 1
  ! printf '%s\n' "$addresses" | grep -Ev '^(127\.0\.0\.1|\[::1\]):11434$' >/dev/null
}

searchapp_macos_start_ollama() {
  local ollama_bin=$1
  local managed_bin=$2
  local models_dir=$3
  local pid_file=$4
  local log_file=$5
  local existing_pid=""

  if [ -f "$pid_file" ]; then
    existing_pid=$(sed -n '1p' "$pid_file" 2>/dev/null || true)
  fi
  case "$existing_pid" in
    ''|*[!0-9]*)
      ;;
    *)
      if kill -0 "$existing_pid" 2>/dev/null; then
        echo "An Ollama process started by this app is already running (PID $existing_pid); waiting for its API."
        return 0
      fi
      ;;
  esac
  rm -f "$pid_file"

  echo "Starting existing macOS Ollama: $ollama_bin"
  if [ "$ollama_bin" = "$managed_bin" ]; then
    env \
      HOME="$HOME" \
      OLLAMA_HOST=127.0.0.1:11434 \
      OLLAMA_MODELS="$models_dir" \
      OLLAMA_NO_CLOUD=1 \
      OLLAMA_NUM_PARALLEL=1 \
      OLLAMA_MAX_LOADED_MODELS=1 \
      OLLAMA_CONTEXT_LENGTH="$OLLAMA_NUM_CTX" \
      OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE" \
      nohup "$ollama_bin" serve >> "$log_file" 2>&1 < /dev/null &
  else
    # Preserve the existing installation's normal model store (usually
    # ~/.ollama). Only process-local serving limits are supplied.
    env \
      HOME="$HOME" \
      OLLAMA_HOST=127.0.0.1:11434 \
      OLLAMA_NO_CLOUD=1 \
      OLLAMA_NUM_PARALLEL=1 \
      OLLAMA_MAX_LOADED_MODELS=1 \
      OLLAMA_CONTEXT_LENGTH="$OLLAMA_NUM_CTX" \
      OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE" \
      nohup "$ollama_bin" serve >> "$log_file" 2>&1 < /dev/null &
  fi
  printf '%s\n' "$!" > "$pid_file"
}
