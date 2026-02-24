from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Optional
import secrets

from fastapi import Request

from .config import settings

# Server-start timestamp used to invalidate sessions across restarts
SERVER_START_TS: int = int(time.time())


def _session_ttl_seconds() -> int:
    hard_ttl = int(settings.session_activity_ttl_seconds or 8 * 60 * 60)
    cookie_ttl = int(settings.session_max_age_seconds or hard_ttl)
    return min(hard_ttl, cookie_ttl)


def generate_session_id() -> str:
    """Generate a UUID7-like hex session id (32 hex chars)."""
    now_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    b = bytearray(16)
    b[0:6] = now_ms.to_bytes(6, "big")
    b[6] = (0x70 | (rand_a >> 8)) & 0x7F
    b[7] = rand_a & 0xFF
    # Set variant bits (10xxxxxx)
    rand_b_bytes = rand_b.to_bytes(8, "big")
    b[8] = (rand_b_bytes[0] & 0x3F) | 0x80
    b[9:16] = rand_b_bytes[1:8]
    return b.hex()


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_session(payload: dict) -> str:
    enriched = dict(payload)
    enriched.setdefault("iat", int(time.time()))
    enriched.setdefault("sv", int(SERVER_START_TS))
    data = json.dumps(enriched, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(settings.secret_key.encode("utf-8"), data, sha256).digest()
    return _b64e(data) + "." + _b64e(sig)


def verify_session(token: str) -> Optional[dict]:
    try:
        data_b64, sig_b64 = token.split(".", 1)
        data = _b64d(data_b64)
        sig = _b64d(sig_b64)
        expected = hmac.new(settings.secret_key.encode("utf-8"), data, sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            return None
        if "user_id" not in obj or "email" not in obj or "sid" not in obj:
            return None
        iat = int(obj.get("iat")) if obj.get("iat") is not None else None
        now = int(time.time())
        if iat is None or (now - iat) > _session_ttl_seconds():
            return None
        sv = int(obj.get("sv")) if obj.get("sv") is not None else None
        if sv is None or sv != SERVER_START_TS:
            return None
        obj["session_id"] = obj.get("sid")
        return obj
    except Exception:
        return None


async def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    return verify_session(token)


def set_session_cookie_headers(token: str) -> dict[str, str]:
    ttl = _session_ttl_seconds()
    attrs = [
        f"{settings.session_cookie_name}={token}",
        "Path=/",
        f"Max-Age={ttl}",
        "HttpOnly",
    ]
    samesite = settings.cookie_samesite or "Lax"
    attrs.append(f"SameSite={samesite}")
    if settings.cookie_secure:
        attrs.append("Secure")
    return {"Set-Cookie": "; ".join(attrs)}


def clear_session_cookie_headers() -> dict[str, str]:
    attrs = [
        f"{settings.session_cookie_name}=null",
        "Path=/",
        "Max-Age=0",
        "HttpOnly",
    ]
    samesite = settings.cookie_samesite or "Lax"
    attrs.append(f"SameSite={samesite}")
    if settings.cookie_secure:
        attrs.append("Secure")
    return {"Set-Cookie": "; ".join(attrs)}