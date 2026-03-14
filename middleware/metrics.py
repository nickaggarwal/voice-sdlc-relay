"""Metrics collection middleware.

Automatically records request durations, message routing events,
agent connection durations, and SSE subscriber counts.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("relay.metrics")


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware that auto-records performance and routing metrics."""

    def __init__(self, app: any) -> None:
        super().__init__(app)
        self._agent_connect_times: dict[str, float] = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.time()
        path = request.url.path
        method = request.method

        # Skip WebSocket upgrades - those are handled separately
        is_ws = request.headers.get("upgrade", "").lower() == "websocket"
        if is_ws:
            return await call_next(request)

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.time() - start) * 1000, 2)
            self._safe_record("request_duration", duration_ms, {"path": path, "method": method, "status": "500"})
            raise

        duration_ms = round((time.time() - start) * 1000, 2)
        status_code = str(response.status_code)

        # Record request duration for all endpoints (skip /health to reduce noise)
        if path != "/health":
            self._safe_record(
                "request_duration",
                duration_ms,
                {"path": path, "method": method, "status": status_code},
            )

        # Track message routing events based on path patterns
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api":
            channel = parts[1]
            endpoint = parts[2]

            # Track prompt submissions
            if endpoint == "prompt" and method == "POST" and response.status_code < 400:
                self._safe_record("message_routed", 1.0, {"channel": channel, "type": "voice_prompt"})

            # Track plan actions
            elif endpoint == "plan-action" and method == "POST" and response.status_code < 400:
                self._safe_record("message_routed", 1.0, {"channel": channel, "type": "plan_action"})

            # Track SSE subscriber connections
            elif endpoint == "events" and method == "GET":
                self._safe_record("sse_subscriber_connected", 1.0, {"channel": channel})

        # Track SSE subscriber counts periodically
        if path == "/health" or path == "/metrics":
            self._record_subscriber_counts()

        return response

    def _record_subscriber_counts(self) -> None:
        """Record current SSE subscriber count as a metric."""
        try:
            import main
            total_subscribers = sum(len(v) for v in main.mobile_subscribers.values())
            self._safe_record("sse_subscriber_count", float(total_subscribers))

            # Record agent connection count
            agent_count = float(len(main.agent_connections))
            self._safe_record("agent_connection_count", agent_count)
        except Exception:
            pass

    @staticmethod
    def _safe_record(name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Record a metric, swallowing any errors to avoid breaking requests."""
        try:
            from routes.metrics import record_metric
            record_metric(name, value, tags)
        except Exception as exc:
            logger.debug("Failed to record metric %s: %s", name, exc)
