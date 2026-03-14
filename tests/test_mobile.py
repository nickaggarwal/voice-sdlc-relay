"""Tests for mobile REST endpoints."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_submit_prompt_queued(client: AsyncClient) -> None:
    """Prompt is queued when no agent is connected."""
    resp = await client.post(
        "/api/test-channel/prompt?secret=test-secret",
        json={"transcript": "Create a login page", "inputType": "voice"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivered"] is False
    assert data["queued"] is True
    assert data["agentOnline"] is False
    assert data["id"]
    assert "queued" in data["message"].lower()


async def test_submit_prompt_delivered(client: AsyncClient, mock_ws: any) -> None:
    """Prompt is delivered when agent is connected."""
    import main

    main.agent_connections["test-channel"] = mock_ws

    resp = await client.post(
        "/api/test-channel/prompt?secret=test-secret",
        json={"transcript": "Build a dashboard", "inputType": "text"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivered"] is True
    assert data["queued"] is False
    assert data["agentOnline"] is True
    assert len(mock_ws.sent) == 1
    assert mock_ws.sent[0]["type"] == "prompt"
    assert mock_ws.sent[0]["transcript"] == "Build a dashboard"


async def test_submit_prompt_missing_secret(client: AsyncClient) -> None:
    """Returns 401 when secret is missing."""
    resp = await client.post(
        "/api/test-channel/prompt",
        json={"transcript": "Hello"},
    )
    assert resp.status_code == 401


async def test_submit_prompt_invalid_secret(client: AsyncClient) -> None:
    """Returns 401 when secret is wrong."""
    resp = await client.post(
        "/api/test-channel/prompt?secret=wrong",
        json={"transcript": "Hello"},
    )
    assert resp.status_code == 401


async def test_submit_prompt_validation_error(client: AsyncClient) -> None:
    """Returns 422 for invalid request body."""
    resp = await client.post(
        "/api/test-channel/prompt?secret=test-secret",
        json={"transcript": "", "inputType": "voice"},
    )
    assert resp.status_code == 422


async def test_submit_plan_action(client: AsyncClient, mock_ws: any) -> None:
    """Plan action is forwarded to agent."""
    import main

    main.agent_connections["ch1"] = mock_ws

    resp = await client.post(
        "/api/ch1/plan-action?secret=test-secret",
        json={"plan_id": "plan-123", "action": "approve"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivered"] is True
    assert data["action"] == "approve"
    assert len(mock_ws.sent) == 1
    assert mock_ws.sent[0]["type"] == "plan_action"
    assert mock_ws.sent[0]["plan_id"] == "plan-123"


async def test_submit_plan_action_with_refinement(client: AsyncClient) -> None:
    """Plan action with refinement text is queued when agent offline."""
    resp = await client.post(
        "/api/ch1/plan-action?secret=test-secret",
        json={
            "plan_id": "plan-456",
            "action": "refine",
            "refinement": "Use TypeScript instead",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["delivered"] is False
    assert data["action"] == "refine"


async def test_submit_plan_action_with_rejection(client: AsyncClient) -> None:
    """Plan action with rejection reason."""
    resp = await client.post(
        "/api/ch1/plan-action?secret=test-secret",
        json={
            "plan_id": "plan-789",
            "action": "reject",
            "rejectionReason": "Too many changes",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "reject"


async def test_submit_plan_action_invalid_action(client: AsyncClient) -> None:
    """Invalid action returns 422."""
    resp = await client.post(
        "/api/ch1/plan-action?secret=test-secret",
        json={"plan_id": "plan-123", "action": "invalid_action"},
    )
    assert resp.status_code == 422


async def test_list_plans_empty(client: AsyncClient) -> None:
    """Empty plan list for new channel."""
    resp = await client.get("/api/ch1/plans?secret=test-secret")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_plans_with_data(client: AsyncClient) -> None:
    """List plans after upserting."""
    import db

    db.upsert_plan("ch1", "p1", "pending", "feature", "Add auth", {"files": 3})
    db.upsert_plan("ch1", "p2", "approved", "bugfix", "Fix login", {"files": 1})

    resp = await client.get("/api/ch1/plans?secret=test-secret")
    assert resp.status_code == 200
    plans = resp.json()
    assert len(plans) == 2


async def test_list_plans_with_status_filter(client: AsyncClient) -> None:
    """Filter plans by status."""
    import db

    db.upsert_plan("ch1", "p1", "pending", "feature", "Add auth")
    db.upsert_plan("ch1", "p2", "approved", "bugfix", "Fix login")

    resp = await client.get("/api/ch1/plans?secret=test-secret&status=pending")
    assert resp.status_code == 200
    plans = resp.json()
    assert len(plans) == 1
    assert plans[0]["status"] == "pending"


async def test_get_single_plan(client: AsyncClient) -> None:
    """Get a single plan by ID."""
    import db

    db.upsert_plan("ch1", "p1", "pending", "feature", "Add auth", {"detail": "full"})

    resp = await client.get("/api/ch1/plans/p1?secret=test-secret")
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["id"] == "p1"
    assert plan["status"] == "pending"
    assert plan["summary"] == "Add auth"


async def test_get_single_plan_not_found(client: AsyncClient) -> None:
    """Returns 404 for non-existent plan."""
    resp = await client.get("/api/ch1/plans/nonexistent?secret=test-secret")
    assert resp.status_code == 404


async def test_cancel_plan(client: AsyncClient) -> None:
    """Cancel an existing plan."""
    import db

    db.upsert_plan("ch1", "p1", "pending", "feature", "Add auth")

    resp = await client.delete("/api/ch1/plans/p1?secret=test-secret")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"

    # Verify plan was updated in DB
    plan = db.get_plan("ch1", "p1")
    assert plan is not None
    assert plan["status"] == "cancelled"


async def test_cancel_plan_not_found(client: AsyncClient) -> None:
    """Returns 404 when cancelling non-existent plan."""
    resp = await client.delete("/api/ch1/plans/nonexistent?secret=test-secret")
    assert resp.status_code == 404


async def test_get_status_no_agent(client: AsyncClient) -> None:
    """Status shows agent offline."""
    resp = await client.get("/api/ch1/status?secret=test-secret")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_online"] is False
    assert data["channel"] == "ch1"
    assert data["pending_prompts"] == 0
    assert data["relay_uptime_s"] > 0


async def test_get_status_with_agent(client: AsyncClient, mock_ws: any) -> None:
    """Status shows agent online when connected."""
    import main

    main.agent_connections["ch1"] = mock_ws
    main.agent_heartbeats["ch1"] = "2024-01-01T00:00:00Z"

    resp = await client.get("/api/ch1/status?secret=test-secret")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_online"] is True
    assert data["last_heartbeat"] == "2024-01-01T00:00:00Z"


async def test_get_status_with_pending_prompts(client: AsyncClient) -> None:
    """Status shows pending prompt count."""
    import db

    db.store_message("ch1", "mobile_to_agent", "prompt", {"type": "prompt", "transcript": "hello"})
    db.store_message("ch1", "mobile_to_agent", "prompt", {"type": "prompt", "transcript": "world"})

    resp = await client.get("/api/ch1/status?secret=test-secret")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pending_prompts"] == 2
