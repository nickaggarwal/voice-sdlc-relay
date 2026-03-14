"""Tests for SSE streaming endpoint."""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_events_requires_secret(client: AsyncClient) -> None:
    """SSE endpoint returns 401 without valid secret."""
    resp = await client.get("/api/ch1/events")
    assert resp.status_code == 401


async def test_events_invalid_secret(client: AsyncClient) -> None:
    """SSE endpoint returns 401 with invalid secret."""
    resp = await client.get("/api/ch1/events?secret=wrong")
    assert resp.status_code == 401


async def test_format_sse() -> None:
    """_format_sse produces valid SSE format."""
    from routes.events import _format_sse

    result = _format_sse("connected", {"channel": "ch1", "agentOnline": False})
    assert result.startswith("event: connected\n")
    assert "data: " in result
    assert result.endswith("\n\n")

    # Parse the data
    lines = result.strip().split("\n")
    assert lines[0] == "event: connected"
    data = json.loads(lines[1].replace("data: ", ""))
    assert data["channel"] == "ch1"
    assert data["agentOnline"] is False


async def test_format_sse_string_data() -> None:
    """_format_sse handles string data without double-encoding."""
    from routes.events import _format_sse

    result = _format_sse("keepalive", "ping")
    assert "data: ping" in result


async def test_event_generator_yields_connection_event() -> None:
    """The event generator yields a connected event first."""
    from routes.events import _event_generator
    import main

    queue: asyncio.Queue = asyncio.Queue()
    main.agent_connections["ch-gen"] = "fake_ws"  # type: ignore

    # Create a mock request
    class MockRequest:
        async def is_disconnected(self) -> bool:
            return True  # Immediately disconnect after connected event

    gen = _event_generator("ch-gen", queue, since=0, request=MockRequest())  # type: ignore

    # First yield should be the connected event
    first_event = await gen.__anext__()
    assert "event: connected" in first_event
    data = json.loads(first_event.split("data: ")[1].strip())
    assert data["channel"] == "ch-gen"
    assert data["agentOnline"] is True


async def test_event_generator_replays_messages() -> None:
    """The event generator replays undelivered messages when since > 0."""
    from routes.events import _event_generator
    import db
    import main

    db.store_message("ch-replay-gen", "agent_to_mobile", "log", {"message": "old log"})

    queue: asyncio.Queue = asyncio.Queue()

    class MockRequest:
        async def is_disconnected(self) -> bool:
            return True

    gen = _event_generator("ch-replay-gen", queue, since=1.0, request=MockRequest())  # type: ignore

    events: list[str] = []
    async for event in gen:
        events.append(event)

    # Should have replayed message + connected event
    assert len(events) >= 2
    assert any("log" in e for e in events)
    assert any("connected" in e for e in events)


async def test_event_generator_marks_replayed_delivered() -> None:
    """Replayed messages are marked as delivered."""
    from routes.events import _event_generator
    import db
    import main

    db.store_message("ch-del-gen", "agent_to_mobile", "status", {"status": "ok"})

    # Verify undelivered
    assert len(db.get_undelivered("ch-del-gen", "agent_to_mobile")) == 1

    queue: asyncio.Queue = asyncio.Queue()

    class MockRequest:
        async def is_disconnected(self) -> bool:
            return True

    gen = _event_generator("ch-del-gen", queue, since=1.0, request=MockRequest())  # type: ignore
    async for _ in gen:
        pass

    # Should be marked delivered now
    assert len(db.get_undelivered("ch-del-gen", "agent_to_mobile")) == 0


async def test_event_generator_yields_queued_events() -> None:
    """The event generator yields events from the queue."""
    from routes.events import _event_generator
    import main

    queue: asyncio.Queue = asyncio.Queue()

    # Pre-load the queue with an event
    await queue.put({"event": "plan_ready", "data": {"plan_id": "p1"}})

    yield_count = 0

    class MockRequest:
        async def is_disconnected(self) -> bool:
            nonlocal yield_count
            # After connected + plan_ready have been yielded (2 yields),
            # the generator checks is_disconnected before trying to read again
            # At that point yield_count will be 2, so disconnect
            return yield_count >= 2

    gen = _event_generator("ch-queue-gen", queue, since=0, request=MockRequest())  # type: ignore

    events: list[str] = []
    async for event in gen:
        events.append(event)
        yield_count += 1

    # Should have connected event + plan_ready event
    assert any("connected" in e for e in events)
    assert any("plan_ready" in e for e in events)


async def test_event_generator_sends_keepalive_on_timeout() -> None:
    """The event generator sends keepalive comments when queue times out."""
    from routes.events import _event_generator
    import main

    queue: asyncio.Queue = asyncio.Queue()

    yield_count = 0

    class MockRequest:
        async def is_disconnected(self) -> bool:
            nonlocal yield_count
            # Disconnect after connected event + keepalive
            return yield_count >= 2

    # Monkey-patch asyncio.wait_for to use a very short timeout
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await original_wait_for(coro, timeout=0.01)

    gen = _event_generator("ch-ka", queue, since=0, request=MockRequest())  # type: ignore

    events: list[str] = []
    asyncio.wait_for = fast_wait_for  # type: ignore
    try:
        async for event in gen:
            events.append(event)
            yield_count += 1
    finally:
        asyncio.wait_for = original_wait_for  # type: ignore

    # Should include a keepalive comment
    assert any(": keepalive" in e for e in events)


async def test_event_generator_cleanup_on_finish() -> None:
    """The event generator removes the subscriber queue on completion."""
    from routes.events import _event_generator
    import main

    queue: asyncio.Queue = asyncio.Queue()
    main.mobile_subscribers["ch-cleanup-gen"] = [queue]

    class MockRequest:
        async def is_disconnected(self) -> bool:
            return True

    gen = _event_generator("ch-cleanup-gen", queue, since=0, request=MockRequest())  # type: ignore
    async for _ in gen:
        pass

    # Queue should be removed from subscribers
    assert "ch-cleanup-gen" not in main.mobile_subscribers


async def test_event_generator_no_replay_when_since_zero() -> None:
    """The event generator skips replay when since=0."""
    from routes.events import _event_generator
    import db
    import main

    db.store_message("ch-no-replay", "agent_to_mobile", "log", {"message": "should not replay"})

    queue: asyncio.Queue = asyncio.Queue()

    class MockRequest:
        async def is_disconnected(self) -> bool:
            return True

    gen = _event_generator("ch-no-replay", queue, since=0, request=MockRequest())  # type: ignore

    events: list[str] = []
    async for event in gen:
        events.append(event)

    # Should only have the connected event, no replay
    assert len(events) == 1
    assert "connected" in events[0]

    # Message should still be undelivered (not replayed)
    assert len(db.get_undelivered("ch-no-replay", "agent_to_mobile")) == 1
