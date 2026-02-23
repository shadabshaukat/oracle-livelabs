from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Optional

from fastapi import Request

from .config import settings

# Server-start timestamp used to invalidate sessions across restarts
SERVER_START_TS: int = int(time.time())
# Hard session TTL (8 hours)
SESSION_TTL_SECONDS: int = 8 * 60 * 60


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
        if "user_id" not in obj or "email" not in obj:
            return None
        iat = int(obj.get("iat")) if obj.get("iat") is not None else None
        now = int(time.time())
        if iat is None or (now - iat) > SESSION_TTL_SECONDS:
            return None
        sv = int(obj.get("sv")) if obj.get("sv") is not None else None
        if sv is None or sv != SERVER_START_TS:
            return None
        return obj
    except Exception:
        return None


async def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    return verify_session(token)


def set_session_cookie_headers(token: str) -> dict[str, str]:
    ttl = min(int(settings.session_max_age_seconds or SESSION_TTL_SECONDS), SESSION_TTL_SECONDS)
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