"""Structured JSON logging middleware for all HTTP requests."""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("relay.access")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Logs each request as structured JSON with method, path, channel, status, and duration."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.time()

        # Extract channel from path if present
        channel = None
        path = request.url.path
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api":
            channel = parts[1]

        # Skip logging for health checks and WebSocket upgrades to reduce noise
        is_ws = request.headers.get("upgrade", "").lower() == "websocket"

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.time() - start) * 1000, 2)
            if not is_ws:
                log_entry = {
                    "method": request.method,
                    "path": path,
                    "channel": channel,
                    "status": 500,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                }
                logger.error(json.dumps(log_entry))
            raise

        duration_ms = round((time.time() - start) * 1000, 2)

        if not is_ws and path != "/health":
            log_entry = {
                "method": request.method,
                "path": path,
                "channel": channel,
                "status": response.status_code,
                "duration_ms": duration_ms,
            }
            if response.status_code >= 400:
                logger.warning(json.dumps(log_entry))
            else:
                logger.info(json.dumps(log_entry))

        return response
