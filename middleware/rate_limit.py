"""In-memory rate limiter per (channel, endpoint)."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# Rate limit configuration: endpoint_suffix -> (max_requests, window_seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/prompt": (30, 60),        # 30 requests per minute per channel
    "/plan-action": (60, 60),   # 60 requests per minute per channel
}


class _TokenBucket:
    """Simple token bucket rate limiter."""

    __slots__ = ("max_tokens", "window", "tokens", "last_refill")

    def __init__(self, max_tokens: int, window: float) -> None:
        self.max_tokens = max_tokens
        self.window = window
        self.tokens = float(max_tokens)
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        """Try to consume a token. Returns True if allowed, False if rate limited."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        # Refill tokens based on elapsed time
        self.tokens = min(
            self.max_tokens,
            self.tokens + (elapsed * self.max_tokens / self.window),
        )
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    @property
    def retry_after(self) -> float:
        """Seconds until the next token is available."""
        if self.tokens >= 1.0:
            return 0.0
        tokens_needed = 1.0 - self.tokens
        return tokens_needed * self.window / self.max_tokens


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limits POST requests per (channel, endpoint)."""

    def __init__(self, app: any) -> None:
        super().__init__(app)
        # Key: (channel, endpoint_suffix) -> TokenBucket
        self._buckets: dict[tuple[str, str], _TokenBucket] = defaultdict(lambda: None)  # type: ignore

    def _get_bucket(self, channel: str, endpoint: str, max_req: int, window: int) -> _TokenBucket:
        """Get or create a token bucket for a (channel, endpoint) pair."""
        key = (channel, endpoint)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(max_req, window)
            self._buckets[key] = bucket
        return bucket

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Only rate limit POST requests
        if request.method != "POST":
            return await call_next(request)

        path = request.url.path

        # Extract channel and endpoint suffix from /api/{channel}/{endpoint}
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "api":
            return await call_next(request)

        channel = parts[1]
        endpoint = "/" + parts[2]

        # Check if this endpoint has a rate limit
        limit_config = RATE_LIMITS.get(endpoint)
        if limit_config is None:
            return await call_next(request)

        max_req, window = limit_config
        bucket = self._get_bucket(channel, endpoint, max_req, window)

        if not bucket.consume():
            retry_after = max(1, int(bucket.retry_after) + 1)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded: {max_req} requests per {window}s for {endpoint}",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
