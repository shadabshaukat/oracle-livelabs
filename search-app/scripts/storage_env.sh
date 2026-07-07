#!/usr/bin/env bash

# Shared writable-path initialization for Linux and macOS. This file is
# sourced by the run/bootstrap/start/stop scripts; it must remain compatible
# with the Bash 3.2 shipped by macOS.

searchapp_expand_home_path() {
  local value=$1
  case "$value" in
    '~')
      printf '%s\n' "$HOME"
      ;;
    '~/'*)
      printf '%s/%s\n' "$HOME" "${value#\~/}"
      ;;
    *)
      printf '%s\n' "$value"
      ;;
  esac
}

searchapp_prepare_storage() {
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    echo "HOME must point to an existing user home directory." >&2
    return 1
  fi

  DATA_DIR=$(searchapp_expand_home_path "${DATA_DIR:-$HOME/.oracle-livelabs/search-app}")
  UPLOAD_DIR=$(searchapp_expand_home_path "${UPLOAD_DIR:-$DATA_DIR/uploads}")
  MODEL_CACHE_DIR=$(searchapp_expand_home_path "${MODEL_CACHE_DIR:-$DATA_DIR/models}")
  SEARCHAPP_LOG_DIR=$(searchapp_expand_home_path "${SEARCHAPP_LOG_DIR:-$DATA_DIR/logs}")
  SEARCHAPP_RUN_DIR=$(searchapp_expand_home_path "${SEARCHAPP_RUN_DIR:-$DATA_DIR/run}")
  SEARCHAPP_RUNTIME_DIR=$(searchapp_expand_home_path "${SEARCHAPP_RUNTIME_DIR:-$DATA_DIR/runtime}")
  STORAGE_BACKEND=$(printf '%s' "${STORAGE_BACKEND:-local}" | tr '[:upper:]' '[:lower:]')

  case "$STORAGE_BACKEND" in
    local)
      ;;
    oci)
      if [ -z "${OCI_OS_BUCKET_NAME:-}" ]; then
        echo "STORAGE_BACKEND=oci requires an explicitly configured OCI_OS_BUCKET_NAME." >&2
        return 1
      fi
      ;;
    s3)
      if [ -z "${S3_BUCKET_NAME:-}" ]; then
        echo "STORAGE_BACKEND=s3 requires an explicitly configured S3_BUCKET_NAME." >&2
        return 1
      fi
      ;;
    both)
      case "${OBJECT_STORAGE_PROVIDER:-}" in
        oci|OCI)
          if [ -z "${OCI_OS_BUCKET_NAME:-}" ]; then
            echo "OBJECT_STORAGE_PROVIDER=oci requires OCI_OS_BUCKET_NAME." >&2
            return 1
          fi
          ;;
        s3|S3)
          if [ -z "${S3_BUCKET_NAME:-}" ]; then
            echo "OBJECT_STORAGE_PROVIDER=s3 requires S3_BUCKET_NAME." >&2
            return 1
          fi
          ;;
        *)
          echo "STORAGE_BACKEND=both requires OBJECT_STORAGE_PROVIDER=oci or s3." >&2
          return 1
          ;;
      esac
      ;;
    *)
      echo "Unsupported STORAGE_BACKEND=$STORAGE_BACKEND. Use local, oci, s3, or both." >&2
      return 1
      ;;
  esac

  local storage_dir
  for storage_dir in \
    "$DATA_DIR" \
    "$UPLOAD_DIR" \
    "$MODEL_CACHE_DIR" \
    "$SEARCHAPP_LOG_DIR" \
    "$SEARCHAPP_RUN_DIR" \
    "$SEARCHAPP_RUNTIME_DIR"; do
    if [ -e "$storage_dir" ] && [ ! -d "$storage_dir" ]; then
      echo "Configured storage path exists but is not a directory: $storage_dir" >&2
      return 1
    fi
    if [ ! -d "$storage_dir" ]; then
      (umask 077 && mkdir -p "$storage_dir") || return 1
    fi
    if [ ! -w "$storage_dir" ]; then
      echo "Configured storage directory is not writable: $storage_dir" >&2
      return 1
    fi
  done

  export DATA_DIR UPLOAD_DIR MODEL_CACHE_DIR STORAGE_BACKEND
  export SEARCHAPP_LOG_DIR SEARCHAPP_RUN_DIR SEARCHAPP_RUNTIME_DIR

  if [ "$STORAGE_BACKEND" = "local" ]; then
    echo "Local file storage ready: $UPLOAD_DIR"
  else
    echo "File storage ready: backend=$STORAGE_BACKEND local_work_dir=$DATA_DIR"
  fi
}
