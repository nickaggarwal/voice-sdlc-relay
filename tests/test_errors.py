"""Tests for the error tracking module: storage, retrieval, and API endpoints."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_errors():
    """Ensure the errors table exists (conftest handles main DB init)."""
    from routes.errors import init_errors_table
    init_errors_table()


# ---------------------------------------------------------------------------
# Unit tests for error storage and retrieval
# ---------------------------------------------------------------------------


class TestStoreError:
    def test_store_and_retrieve(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        error_id = store_error(
            channel="test-channel",
            component="validator",
            error="Something went wrong",
            traceback_str="Traceback...\n  File test.py",
            context={"plan_id": "plan-123"},
        )

        assert error_id is not None
        assert error_id > 0

        errors = get_errors("test-channel")
        assert len(errors) == 1
        assert errors[0]["channel"] == "test-channel"
        assert errors[0]["component"] == "validator"
        assert errors[0]["error"] == "Something went wrong"
        assert errors[0]["traceback"] == "Traceback...\n  File test.py"
        assert errors[0]["context"] == {"plan_id": "plan-123"}

    def test_store_without_optional_fields(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        error_id = store_error(
            channel="ch1",
            component="deployer",
            error="Deploy failed",
        )

        errors = get_errors("ch1")
        assert len(errors) == 1
        assert errors[0]["traceback"] is None
        assert errors[0]["context"] == {}

    def test_multiple_errors_ordering(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        store_error("ch2", "comp1", "First error")
        store_error("ch2", "comp2", "Second error")
        store_error("ch2", "comp3", "Third error")

        errors = get_errors("ch2")
        assert len(errors) == 3
        # Most recent first
        assert errors[0]["error"] == "Third error"
        assert errors[2]["error"] == "First error"

    def test_channel_isolation(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        store_error("channel-a", "comp", "Error A")
        store_error("channel-b", "comp", "Error B")

        errors_a = get_errors("channel-a")
        errors_b = get_errors("channel-b")

        assert len(errors_a) == 1
        assert errors_a[0]["error"] == "Error A"
        assert len(errors_b) == 1
        assert errors_b[0]["error"] == "Error B"


class TestGetErrors:
    def test_limit(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        for i in range(10):
            store_error("limit-ch", "comp", f"Error {i}")

        errors = get_errors("limit-ch", limit=3)
        assert len(errors) == 3

    def test_since_filter(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_errors

        store_error("since-ch", "comp", "Old error")
        future = time.time() + 1000
        errors = get_errors("since-ch", since=future)
        assert len(errors) == 0

    def test_empty_result(self) -> None:
        _init_errors()
        from routes.errors import get_errors

        errors = get_errors("nonexistent-channel")
        assert len(errors) == 0


class TestGetRecentErrors:
    def test_recent_across_channels(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_recent_errors

        store_error("ch-x", "comp", "Error X")
        store_error("ch-y", "comp", "Error Y")
        store_error("ch-z", "comp", "Error Z")

        recent = get_recent_errors(limit=10)
        assert len(recent) == 3
        channels = {e["channel"] for e in recent}
        assert channels == {"ch-x", "ch-y", "ch-z"}

    def test_recent_limit(self) -> None:
        _init_errors()
        from routes.errors import store_error, get_recent_errors

        for i in range(20):
            store_error(f"ch-{i}", "comp", f"Error {i}")

        recent = get_recent_errors(limit=5)
        assert len(recent) == 5


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_error(client: AsyncClient) -> None:
    """POST /api/{channel}/errors should store an error report."""
    _init_errors()

    response = await client.post(
        "/api/test-ch/errors?secret=test-secret",
        json={
            "component": "validator",
            "error": "Container build failed",
            "traceback": "at line 42",
            "context": {"plan_id": "abc-123"},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["id"] > 0


@pytest.mark.asyncio
async def test_post_error_minimal(client: AsyncClient) -> None:
    """POST /api/{channel}/errors with only required fields should work."""
    _init_errors()

    response = await client.post(
        "/api/min-ch/errors?secret=test-secret",
        json={
            "component": "deployer",
            "error": "Deploy timeout",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True


@pytest.mark.asyncio
async def test_post_error_auth_required(client: AsyncClient) -> None:
    """POST /api/{channel}/errors without secret should return 401."""
    _init_errors()

    response = await client.post(
        "/api/test-ch/errors",
        json={"component": "test", "error": "test error"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_errors_endpoint(client: AsyncClient) -> None:
    """GET /api/{channel}/errors should return stored errors."""
    _init_errors()
    from routes.errors import store_error

    store_error("get-ch", "validator", "Error 1")
    store_error("get-ch", "deployer", "Error 2")

    response = await client.get("/api/get-ch/errors?secret=test-secret")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["error"] == "Error 2"  # Most recent first
    assert data[1]["error"] == "Error 1"


@pytest.mark.asyncio
async def test_get_errors_with_limit(client: AsyncClient) -> None:
    """GET /api/{channel}/errors?limit=1 should respect the limit."""
    _init_errors()
    from routes.errors import store_error

    for i in range(5):
        store_error("lim-ch", "comp", f"Error {i}")

    response = await client.get("/api/lim-ch/errors?secret=test-secret&limit=2")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_get_errors_auth_required(client: AsyncClient) -> None:
    """GET /api/{channel}/errors without secret should return 401."""
    _init_errors()

    response = await client.get("/api/test-ch/errors")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_roundtrip(client: AsyncClient) -> None:
    """POST then GET should show the same error."""
    _init_errors()

    # Post an error
    post_resp = await client.post(
        "/api/rt-ch/errors?secret=test-secret",
        json={
            "component": "autofix",
            "error": "Fix attempt exceeded max retries",
            "context": {"attempt": 3, "max_retries": 3},
        },
    )
    assert post_resp.status_code == 200

    # Retrieve it
    get_resp = await client.get("/api/rt-ch/errors?secret=test-secret")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data) == 1
    assert data[0]["component"] == "autofix"
    assert data[0]["error"] == "Fix attempt exceeded max retries"
    assert data[0]["context"]["attempt"] == 3
