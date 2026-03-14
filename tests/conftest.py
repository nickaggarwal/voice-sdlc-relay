"""Shared pytest fixtures for relay server tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set test environment variables before importing app modules
os.environ["RELAY_SECRET"] = "test-secret"
os.environ["LOG_LEVEL"] = "warning"


@pytest.fixture(autouse=True)
def _test_db(tmp_path: any) -> Generator[None, None, None]:
    """Use a temporary database for each test."""
    db_file = str(tmp_path / "test_relay.db")
    os.environ["DB_PATH"] = db_file

    # Patch db module to use the new path
    import db
    db.DB_PATH = db_file
    db.init_db()

    # Initialize observability tables
    from routes.metrics import init_metrics_table
    from routes.errors import init_errors_table
    init_metrics_table()
    init_errors_table()

    yield

    # Cleanup
    try:
        os.unlink(db_file)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _reset_state() -> Generator[None, None, None]:
    """Reset in-memory state between tests."""
    import main

    main.agent_connections.clear()
    main.mobile_subscribers.clear()
    main.agent_heartbeats.clear()
    main.start_time = 1000000000.0  # Fixed start time for predictable uptime

    yield

    main.agent_connections.clear()
    main.mobile_subscribers.clear()
    main.agent_heartbeats.clear()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client for the FastAPI app."""
    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


class MockWebSocket:
    """Mock WebSocket for testing agent interactions."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: bool = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send_json(self, data: dict) -> None:
        if self.closed:
            raise RuntimeError("WebSocket is closed")
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


@pytest.fixture
def mock_ws() -> MockWebSocket:
    """Create a mock WebSocket instance."""
    return MockWebSocket()
