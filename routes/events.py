"""Server-Sent Events (SSE) stream for mobile clients."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from auth import verify_secret
from db import get_undelivered, mark_delivered

router = APIRouter(prefix="/api/{channel}", tags=["events"])

logger = logging.getLogger(__name__)


def _get_state() -> tuple[dict, dict, dict]:
    """Import shared state from main module."""
    import main

    return main.agent_connections, main.mobile_subscribers, main.agent_heartbeats


def _format_sse(event: str, data: Any) -> str:
    """Format a server-sent event string."""
    data_str = json.dumps(data) if not isinstance(data, str) else data
    return f"event: {event}\ndata: {data_str}\n\n"


async def _event_generator(
    channel: str,
    queue: asyncio.Queue[dict[str, Any]],
    since: float,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Generate SSE events for a channel subscriber."""
    connections, subscribers, heartbeats = _get_state()

    # Replay missed messages from SQLite if since > 0
    if since > 0:
        from datetime import datetime, timezone

        since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        missed = get_undelivered(channel, "agent_to_mobile", since=since_str)
        delivered_ids = []
        for msg in missed:
            yield _format_sse(msg["msg_type"], msg["payload"])
            delivered_ids.append(msg["id"])
        if delivered_ids:
            mark_delivered(delivered_ids)

    # Send connection event with agent status
    agent_online = channel in connections
    yield _format_sse("connected", {
        "channel": channel,
        "agentOnline": agent_online,
        "lastHeartbeat": heartbeats.get(channel),
    })

    # Streaming loop
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield _format_sse(event["event"], event["data"])
            except asyncio.TimeoutError:
                # Send keepalive comment
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        # Remove queue from subscribers
        queues = subscribers.get(channel, [])
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            subscribers.pop(channel, None)


@router.get("/events")
async def event_stream(
    channel: str,
    request: Request,
    secret: str | None = Query(default=None),
    since: float = Query(default=0),
) -> StreamingResponse:
    """SSE stream of events for a channel."""
    verify_secret(secret)

    _, subscribers, _ = _get_state()

    # Create a new queue for this subscriber
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)

    if channel not in subscribers:
        subscribers[channel] = []
    subscribers[channel].append(queue)

    logger.info(f"SSE subscriber connected: channel={channel}")

    return StreamingResponse(
        _event_generator(channel, queue, since, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
