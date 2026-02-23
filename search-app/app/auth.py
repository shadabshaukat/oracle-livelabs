from __future__ import annotations

import base64
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .session import verify_session


class SessionOrBasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, protect_paths: tuple[str, ...] = ("/api", "/docs", "/openapi.json", "/redoc")):
        super().__init__(app)
        self.protect_paths = protect_paths

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path or "/"
        public = {"/api/health", "/api/ready", "/api/login", "/api/register", "/api/llm-config", "/api/llm-test", "/api/llm-debug"}
        if any(path.startswith(prefix) for prefix in self.protect_paths):
            if path in public:
                return await call_next(request)
            # 1) Session cookie
            tok = request.cookies.get(settings.session_cookie_name)
            if tok and verify_session(tok):
                return await call_next(request)
            # 2) Basic auth fallback
            auth = request.headers.get("Authorization")
            if auth and auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    if username == settings.basic_auth_user and password == settings.basic_auth_password:
                        return await call_next(request)
                except Exception:
                    pass
            return self._unauthorized()
        return await call_next(request)

    @staticmethod
    def _unauthorized() -> Response:
        return Response(
            status_code=401,
            content="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Restricted\""},
            media_type="text/plain",
        )
