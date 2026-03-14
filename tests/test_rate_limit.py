"""Tests for rate limiting middleware."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_prompt_rate_limit_allows_normal_usage(client: AsyncClient) -> None:
    """Normal usage within rate limits is allowed."""
    for i in range(5):
        resp = await client.post(
            "/api/ch1/prompt?secret=test-secret",
            json={"transcript": f"Message {i}", "inputType": "voice"},
        )
        assert resp.status_code == 200, f"Request {i} should succeed"


async def test_prompt_rate_limit_enforced(client: AsyncClient) -> None:
    """Rate limit is enforced after exceeding threshold."""
    # Send 30 requests (the limit for /prompt)
    for i in range(30):
        resp = await client.post(
            "/api/ch-rate/prompt?secret=test-secret",
            json={"transcript": f"Message {i}", "inputType": "text"},
        )
        assert resp.status_code == 200, f"Request {i} should succeed"

    # The 31st request should be rate limited
    resp = await client.post(
        "/api/ch-rate/prompt?secret=test-secret",
        json={"transcript": "One too many", "inputType": "text"},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Rate limit" in data["detail"]
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


async def test_plan_action_rate_limit_allows_more(client: AsyncClient) -> None:
    """Plan action has a higher rate limit (60/min)."""
    import db

    # Create a plan to act on
    db.upsert_plan("ch-plan-rate", "p1", "pending")

    for i in range(50):
        resp = await client.post(
            "/api/ch-plan-rate/plan-action?secret=test-secret",
            json={"plan_id": "p1", "action": "approve"},
        )
        assert resp.status_code == 200, f"Request {i} should succeed"


async def test_rate_limit_per_channel(client: AsyncClient) -> None:
    """Rate limits are tracked per channel, not globally."""
    # Exhaust rate limit on ch-a
    for i in range(30):
        await client.post(
            "/api/ch-a/prompt?secret=test-secret",
            json={"transcript": f"Msg {i}", "inputType": "text"},
        )

    # ch-a should be rate limited
    resp = await client.post(
        "/api/ch-a/prompt?secret=test-secret",
        json={"transcript": "Blocked", "inputType": "text"},
    )
    assert resp.status_code == 429

    # ch-b should still work
    resp = await client.post(
        "/api/ch-b/prompt?secret=test-secret",
        json={"transcript": "Allowed", "inputType": "text"},
    )
    assert resp.status_code == 200


async def test_rate_limit_not_applied_to_get(client: AsyncClient) -> None:
    """GET requests are not rate limited."""
    for i in range(50):
        resp = await client.get("/api/ch1/status?secret=test-secret")
        assert resp.status_code == 200


async def test_rate_limit_429_response_format(client: AsyncClient) -> None:
    """429 response includes proper detail and retry_after fields."""
    # Exhaust the limit
    for i in range(30):
        await client.post(
            "/api/ch-format/prompt?secret=test-secret",
            json={"transcript": f"Msg {i}", "inputType": "text"},
        )

    resp = await client.post(
        "/api/ch-format/prompt?secret=test-secret",
        json={"transcript": "Blocked", "inputType": "text"},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "detail" in data
    assert "retry_after" in data
    assert isinstance(data["retry_after"], int)
    assert data["retry_after"] >= 1


async def test_rate_limit_not_applied_to_non_api_routes(client: AsyncClient) -> None:
    """Non-API routes are not rate limited."""
    for i in range(50):
        resp = await client.get("/health")
        assert resp.status_code == 200
