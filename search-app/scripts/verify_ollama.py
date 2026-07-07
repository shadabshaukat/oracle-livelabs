#!/usr/bin/env python3
"""Fail-closed verification for the pinned local Ollama runtime and model."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_VERSION = "0.31.1"
DEFAULT_MODEL = "ibm/granite4:1b-q4_K_M"
DEFAULT_DIGEST = "2ab52bcf721423ba9f96d63f618716006228572ec71eac43ad7187ec654af824"


def _json_request(url: str, *, payload: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return parsed


def _matching_model(data: dict[str, Any], model: str, digest: str) -> dict[str, Any] | None:
    models = data.get("models") or []
    return next(
        (
            item
            for item in models
            if isinstance(item, dict)
            and (item.get("name") == model or item.get("model") == model)
            and str(item.get("digest") or "") == digest
        ),
        None,
    )


def _keep_alive_value(value: str) -> str | int:
    value = value.strip()
    if value in {"-1", "0"}:
        return int(value)
    return value


def _ensure_loaded(base_url: str, model: str, digest: str, *, num_ctx: int, keep_alive: str, timeout: float) -> None:
    ps_data = _json_request(f"{base_url}/api/ps")
    resident = _matching_model(ps_data, model, digest)
    if resident is not None and int(resident.get("context_length") or 0) == num_ctx:
        print(f"Ollama model already loaded: {model} (context={num_ctx})")
        return

    if resident is not None:
        # A loaded model keeps its original context allocation. Unload only when
        # the configured context changed, then preload the exact requested shape.
        _json_request(
            f"{base_url}/api/generate",
            payload={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            timeout=timeout,
        )

    print(f"Loading Ollama model into memory: {model} (context={num_ctx})")
    _json_request(
        f"{base_url}/api/generate",
        payload={
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": _keep_alive_value(keep_alive),
            "options": {"num_ctx": num_ctx},
        },
        timeout=timeout,
    )

    deadline = time.monotonic() + 15
    while True:
        resident = _matching_model(_json_request(f"{base_url}/api/ps"), model, digest)
        if resident is not None and int(resident.get("context_length") or 0) == num_ctx:
            print(f"Ollama model loaded: {model} (context={num_ctx})")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    raise RuntimeError(f"Pinned Ollama model did not become resident with context_length={num_ctx}: {model}")


def verify(
    base_url: str,
    version: str,
    model: str,
    digest: str,
    *,
    smoke: bool = False,
    ensure_loaded: bool = False,
) -> None:
    base_url = base_url.rstrip("/")
    version_data = _json_request(f"{base_url}/api/version")
    actual_version = str(version_data.get("version") or "")
    if actual_version != version:
        raise RuntimeError(f"Ollama version mismatch: expected {version}, got {actual_version or 'unknown'}")

    tags_data = _json_request(f"{base_url}/api/tags")
    models = tags_data.get("models") or []
    match = next(
        (item for item in models if isinstance(item, dict) and (item.get("name") == model or item.get("model") == model)),
        None,
    )
    if match is None:
        raise RuntimeError(f"Pinned Ollama model is not installed: {model}")
    actual_digest = str(match.get("digest") or "")
    if actual_digest != digest:
        raise RuntimeError(f"Model digest mismatch for {model}: expected {digest}, got {actual_digest or 'unknown'}")
    quantization = str((match.get("details") or {}).get("quantization_level") or "")
    if quantization.upper() != "Q4_K_M":
        raise RuntimeError(f"Model quantization mismatch: expected Q4_K_M, got {quantization or 'unknown'}")

    timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
    keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "-1")
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    if ensure_loaded:
        _ensure_loaded(
            base_url,
            model,
            digest,
            num_ctx=num_ctx,
            keep_alive=keep_alive,
            timeout=timeout,
        )

    if smoke:
        smoke_data = _json_request(
            f"{base_url}/api/chat",
            payload={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly LOCAL_OLLAMA_OK"}],
                "stream": False,
                "think": False,
                "keep_alive": _keep_alive_value(keep_alive),
                "options": {"temperature": 0, "num_predict": 24, "num_ctx": num_ctx},
            },
            timeout=timeout,
        )
        message = smoke_data.get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content:
            raise RuntimeError("Ollama smoke inference returned an empty response")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--version", default=os.getenv("OLLAMA_VERSION", DEFAULT_VERSION))
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--digest", default=os.getenv("OLLAMA_MODEL_DIGEST", DEFAULT_DIGEST))
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--ensure-loaded",
        action="store_true",
        help="Keep the exact pinned model resident, preloading it only when needed.",
    )
    args = parser.parse_args()
    try:
        verify(
            args.base_url,
            args.version,
            args.model,
            args.digest,
            smoke=args.smoke,
            ensure_loaded=args.ensure_loaded,
        )
    except Exception as exc:
        print(f"Ollama verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Ollama verified: version={args.version} model={args.model} digest={args.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
