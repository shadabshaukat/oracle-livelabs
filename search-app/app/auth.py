from __future__ import annotations

import base64
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, protect_paths: tuple[str, ...] = ("/api", "/ui", "/docs", "/openapi.json", "/redoc")):
        super().__init__(app)
        self.protect_paths = protect_paths

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path or "/"
        if any(path.startswith(prefix) for prefix in self.protect_paths):
            auth = request.headers.get("Authorization")
            if not auth or not auth.startswith("Basic "):
                return self._unauthorized()
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                username, password = decoded.split(":", 1)
            except Exception:
                return self._unauthorized()

            if username != settings.basic_auth_user or password != settings.basic_auth_password:
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
