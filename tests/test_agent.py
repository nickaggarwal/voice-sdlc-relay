"""Tests for WebSocket agent connections."""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_agent_ws_invalid_secret(client: AsyncClient) -> None:
    """WebSocket rejects connection with invalid secret."""
    from starlette.testclient import TestClient
    from main import app

    test_client = TestClient(app)
    with pytest.raises(Exception):
        with test_client.websocket_connect("/ws/ch1?secret=wrong"):
            pass


async def test_agent_ws_no_secret(client: AsyncClient) -> None:
    """WebSocket rejects connection without secret."""
    from starlette.testclient import TestClient
    from main import app

    test_client = TestClient(app)
    with pytest.raises(Exception):
        with test_client.websocket_connect("/ws/ch1"):
            pass


async def test_agent_ws_connects(client: AsyncClient) -> None:
    """Agent can connect via WebSocket with valid secret."""
    from starlette.testclient import TestClient
    from main import app
    import main

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        # Agent should be registered
        assert "ch1" in main.agent_connections

    # After disconnect, agent should be removed
    assert "ch1" not in main.agent_connections


async def test_agent_ws_heartbeat(client: AsyncClient) -> None:
    """Agent heartbeat receives ack and updates timestamp."""
    from starlette.testclient import TestClient
    from main import app
    import main

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        ws.send_json({"type": "heartbeat"})
        ack = ws.receive_json()
        assert ack["type"] == "heartbeat_ack"
        assert "timestamp" in ack
        assert "ch1" in main.agent_heartbeats


async def test_agent_ws_plan_ready(client: AsyncClient) -> None:
    """Agent sending plan_ready stores the plan in DB."""
    from starlette.testclient import TestClient
    from main import app
    import db

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        ws.send_json({
            "type": "plan_ready",
            "plan": {
                "plan_id": "p1",
                "status": "pending",
                "change_type": "feature",
                "summary": "Add login page",
                "changes": [{"file": "login.tsx", "action": "create"}],
            },
        })
        # Give it a moment to process
        import time
        time.sleep(0.1)

    # Check plan was stored in DB
    plan = db.get_plan("ch1", "p1")
    assert plan is not None
    assert plan["status"] == "pending"
    assert plan["summary"] == "Add login page"
    assert plan["change_type"] == "feature"


async def test_agent_ws_plan_updated(client: AsyncClient) -> None:
    """Agent sending plan_updated upserts the plan."""
    from starlette.testclient import TestClient
    from main import app
    import db

    db.upsert_plan("ch1", "p1", "pending", "feature", "Add login")

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        ws.send_json({
            "type": "plan_updated",
            "plan": {
                "plan_id": "p1",
                "status": "in_progress",
                "summary": "Updated login with OAuth",
            },
        })
        import time
        time.sleep(0.1)

    plan = db.get_plan("ch1", "p1")
    assert plan is not None
    assert plan["status"] == "in_progress"
    assert plan["summary"] == "Updated login with OAuth"


async def test_agent_ws_message_stored(client: AsyncClient) -> None:
    """Non-heartbeat messages are stored in the database."""
    from starlette.testclient import TestClient
    from main import app
    import db

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        ws.send_json({
            "type": "status_update",
            "status": "working",
            "message": "Implementing feature...",
        })
        import time
        time.sleep(0.1)

    # Check message was stored
    messages = db.get_undelivered("ch1", "agent_to_mobile")
    assert len(messages) >= 1
    found = any(m["msg_type"] == "status_update" for m in messages)
    assert found


async def test_agent_ws_pushes_to_mobile(client: AsyncClient) -> None:
    """Messages from agent are pushed to mobile SSE subscribers."""
    from starlette.testclient import TestClient
    from main import app
    import main

    # Set up a mock subscriber queue
    q: asyncio.Queue = asyncio.Queue()
    main.mobile_subscribers["ch1"] = [q]

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        # The connect event should have been pushed (agent_status online)
        event = q.get_nowait()
        assert event["event"] == "agent_status"
        assert event["data"]["online"] is True

        ws.send_json({
            "type": "log",
            "message": "Starting build...",
        })
        import time
        time.sleep(0.1)

    # Should have received the log message
    log_event = q.get_nowait()
    assert log_event["event"] == "log"
    assert log_event["data"]["message"] == "Starting build..."

    # Should have received agent_status offline on disconnect
    offline_event = q.get_nowait()
    assert offline_event["event"] == "agent_status"
    assert offline_event["data"]["online"] is False


async def test_agent_ws_delivers_queued(client: AsyncClient) -> None:
    """Agent receives queued messages on connect."""
    from starlette.testclient import TestClient
    from main import app
    import db

    # Queue a message
    db.store_message("ch1", "mobile_to_agent", "prompt", {
        "type": "prompt",
        "id": "msg-1",
        "transcript": "Build a dashboard",
    })

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws:
        # Agent should receive the queued message
        msg = ws.receive_json()
        assert msg["type"] == "prompt"
        assert msg["transcript"] == "Build a dashboard"


async def test_agent_ws_last_writer_wins(client: AsyncClient) -> None:
    """Second agent on same channel replaces the first."""
    from starlette.testclient import TestClient
    from main import app
    import main

    test_client = TestClient(app)
    with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws1:
        assert "ch1" in main.agent_connections
        first_ws = main.agent_connections["ch1"]

        with test_client.websocket_connect("/ws/ch1?secret=test-secret") as ws2:
            # Second connection should have replaced the first
            assert main.agent_connections["ch1"] is not first_ws
