"""Simple in-memory rate limiter middleware.

No external dependencies. Uses a sliding window per client IP.
Limits are per-minute. Configurable via env vars:
  RATE_LIMIT_AUTH     — /api/v1/admin/auth (default: 300, was 30)
  RATE_LIMIT_DEFAULT  — all other paths       (default: 120)
"""
import os
import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting with configurable limits per path prefix."""

    def __init__(self, app, limits: dict[str, int] = None, default_limit: int = 120):
        """
        limits: dict of path_prefix → requests_per_minute
        default_limit: fallback if no prefix matches
        """
        super().__init__(app)
        self.limits = limits or {}
        self.default_limit = default_limit
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _get_limit(self, path: str) -> int:
        for prefix, limit in self.limits.items():
            if path.startswith(prefix):
                return limit
        return self.default_limit

    def _clean_window(self, key: str, now: float) -> int:
        """Remove entries older than 60s, return current count."""
        window = self._requests[key]
        cutoff = now - 60
        self._requests[key] = [t for t in window if t > cutoff]
        return len(self._requests[key])

    async def dispatch(self, request, call_next):
        # Don't rate limit health checks, localhost, or /me (session verification)
        if request.url.path in ("/health", "/health/ready", "/api/v1/admin/auth/me"):
            return await call_next(request)

        ip = request.headers.get("X-Real-IP") or request.client.host
        if ip in ("127.0.0.1", "::1", "localhost"):
            return await call_next(request)
        path = request.url.path
        limit = self._get_limit(path)
        key = f"{ip}:{path.split('/')[1] if '/' in path[1:] else path}"

        now = time.time()
        count = self._clean_window(key, now)

        if count >= limit:
            return JSONResponse(
                {"error": "rate limit exceeded", "retry_after": 60},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        self._requests[key].append(now)
        return await call_next(request)
