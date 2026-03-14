"""Voice SDLC Relay Server - Thin message router between mobile clients and coding agents."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from db import cleanup_old, init_db
from middleware.logging import StructuredLoggingMiddleware
from middleware.metrics import MetricsMiddleware
from middleware.rate_limit import RateLimitMiddleware
from routes import agent, dashboard, errors, events, metrics, mobile

# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

# channel -> WebSocket connection for the agent
agent_connections: dict[str, WebSocket] = {}

# channel -> list of asyncio.Queue for SSE subscribers
mobile_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

# channel -> ISO timestamp of last agent heartbeat
agent_heartbeats: dict[str, str] = {}

# Server start time for uptime calculation
start_time: float = 0.0


async def _cleanup_loop() -> None:
    """Background task that periodically cleans up old messages and plans."""
    max_age = int(os.environ.get("MAX_MESSAGE_AGE_HOURS", "24"))
    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            deleted = cleanup_old(max_age)
            if deleted > 0:
                logging.info(f"Cleanup: removed {deleted} old records")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.error(f"Cleanup error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown lifecycle."""
    global start_time
    start_time = time.time()

    # Initialize database
    init_db()

    # Initialize observability tables
    from routes.metrics import init_metrics_table
    from routes.errors import init_errors_table
    init_metrics_table()
    init_errors_table()
    logging.info("Database initialized (including metrics and errors tables)")

    # Start background cleanup task
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logging.info("Relay server started")

    yield

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logging.info("Relay server stopped")


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Voice SDLC Relay",
    description="Thin message router between mobile clients and coding agents",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware - allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom middleware
app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(RateLimitMiddleware)

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

# Include routers
app.include_router(dashboard.router)
app.include_router(mobile.router)
app.include_router(events.router)
app.include_router(agent.router)
app.include_router(metrics.router)
app.include_router(errors.router)
