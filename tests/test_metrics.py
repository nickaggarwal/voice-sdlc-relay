"""Tests for the metrics module: recording, querying, summaries, and endpoints."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_metrics():
    """Ensure the metrics table exists (conftest handles main DB init)."""
    from routes.metrics import init_metrics_table
    init_metrics_table()


# ---------------------------------------------------------------------------
# Unit tests for metric recording and querying
# ---------------------------------------------------------------------------


class TestRecordMetric:
    def test_record_and_get(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metrics

        record_metric("test_latency", 42.5, {"env": "test"})
        results = get_metrics("test_latency")

        assert len(results) == 1
        assert results[0]["name"] == "test_latency"
        assert results[0]["value"] == 42.5
        assert results[0]["tags"] == {"env": "test"}

    def test_record_multiple_and_order(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metrics

        record_metric("multi", 10.0)
        record_metric("multi", 20.0)
        record_metric("multi", 30.0)

        results = get_metrics("multi")
        assert len(results) == 3
        # Most recent first
        assert results[0]["value"] == 30.0
        assert results[2]["value"] == 10.0

    def test_get_with_since_filter(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metrics

        # Record one metric
        record_metric("filtered", 100.0)
        cutoff = time.time() + 1  # Future cutoff

        # Record another metric (in the future, effectively)
        record_metric("filtered", 200.0)

        # All should be returned with no since
        all_results = get_metrics("filtered")
        assert len(all_results) == 2

        # With a future cutoff, nothing should match
        future_results = get_metrics("filtered", since=cutoff + 100)
        assert len(future_results) == 0

    def test_get_with_limit(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metrics

        for i in range(10):
            record_metric("limited", float(i))

        results = get_metrics("limited", limit=3)
        assert len(results) == 3

    def test_record_without_tags(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metrics

        record_metric("no_tags", 55.5)
        results = get_metrics("no_tags")

        assert len(results) == 1
        assert results[0]["tags"] == {}


class TestGetMetricSummary:
    def test_summary_basic(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metric_summary

        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        for v in values:
            record_metric("summary_test", v)

        summary = get_metric_summary("summary_test")

        assert summary["count"] == 5
        assert summary["min"] == 10.0
        assert summary["max"] == 50.0
        assert summary["avg"] == 30.0
        assert summary["p50"] >= 20.0
        assert summary["p95"] >= 40.0

    def test_summary_empty(self) -> None:
        _init_metrics()
        from routes.metrics import get_metric_summary

        summary = get_metric_summary("nonexistent")

        assert summary["count"] == 0
        assert summary["min"] == 0
        assert summary["max"] == 0
        assert summary["avg"] == 0

    def test_summary_single_value(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metric_summary

        record_metric("single", 42.0)
        summary = get_metric_summary("single")

        assert summary["count"] == 1
        assert summary["min"] == 42.0
        assert summary["max"] == 42.0
        assert summary["avg"] == 42.0

    def test_summary_with_since(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, get_metric_summary

        record_metric("since_test", 100.0)
        record_metric("since_test", 200.0)

        # All data
        summary = get_metric_summary("since_test")
        assert summary["count"] == 2

        # Future cutoff should return empty
        future_summary = get_metric_summary("since_test", since=time.time() + 1000)
        assert future_summary["count"] == 0


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_html_endpoint(client: AsyncClient) -> None:
    """GET /metrics should return an HTML page with the metrics dashboard."""
    _init_metrics()
    from routes.metrics import record_metric

    record_metric("prompt_to_plan_latency", 1500.0)
    record_metric("validation_pass", 1.0)
    record_metric("deploy_success", 1.0)

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Voice SDLC Metrics" in body
    assert "Validation Pass Rate" in body
    assert "Deploy Success Rate" in body
    assert "auto-refreshes every 30 seconds" in body


@pytest.mark.asyncio
async def test_metrics_html_empty(client: AsyncClient) -> None:
    """GET /metrics should work even with no data recorded."""
    _init_metrics()
    response = await client.get("/metrics")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_metrics_endpoint(client: AsyncClient) -> None:
    """GET /api/metrics should return JSON metric summaries."""
    _init_metrics()
    from routes.metrics import record_metric

    record_metric("prompt_to_plan_latency", 1000.0)
    record_metric("prompt_to_plan_latency", 2000.0)
    record_metric("validation_pass", 1.0)
    record_metric("validation_fail", 1.0)
    record_metric("deploy_success", 1.0)

    response = await client.get("/api/metrics")

    assert response.status_code == 200
    data = response.json()

    assert "prompt_to_plan_latency" in data
    assert data["prompt_to_plan_latency"]["count"] == 2
    assert data["prompt_to_plan_latency"]["avg"] == 1500.0

    assert "validation_pass_rate" in data
    assert data["validation_pass_rate"]["total"] == 2
    assert data["validation_pass_rate"]["passed"] == 1
    assert data["validation_pass_rate"]["rate"] == 0.5

    assert "deploy_success_rate" in data
    assert data["deploy_success_rate"]["total"] == 1
    assert data["deploy_success_rate"]["succeeded"] == 1
    assert data["deploy_success_rate"]["rate"] == 1.0

    assert "queue_depth" in data
    assert "agent_uptime" in data


@pytest.mark.asyncio
async def test_api_metrics_since_hours(client: AsyncClient) -> None:
    """GET /api/metrics?since_hours=1 should limit the time window."""
    _init_metrics()
    response = await client.get("/api/metrics?since_hours=1")
    assert response.status_code == 200
    data = response.json()
    assert "prompt_to_plan_latency" in data


class TestRateSummary:
    def test_validation_rate(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, _get_rate_summary

        for _ in range(8):
            record_metric("validation_pass", 1.0)
        for _ in range(2):
            record_metric("validation_fail", 1.0)

        result = _get_rate_summary("validation_pass", "validation_fail")
        assert result["total"] == 10
        assert result["passed"] == 8
        assert result["failed"] == 2
        assert result["rate"] == 0.8

    def test_empty_rate(self) -> None:
        _init_metrics()
        from routes.metrics import _get_rate_summary

        result = _get_rate_summary("no_pass", "no_fail")
        assert result["total"] == 0
        assert result["rate"] == 0.0


class TestQueueDepth:
    def test_queue_depth(self) -> None:
        _init_metrics()
        from routes.metrics import _get_queue_depth
        from db import store_message

        # Store some undelivered messages
        store_message("ch1", "mobile_to_agent", "prompt", {"text": "hello"})
        store_message("ch1", "mobile_to_agent", "prompt", {"text": "world"})

        depth = _get_queue_depth()
        assert depth >= 2


class TestAgentUptime:
    def test_uptime_no_heartbeats(self) -> None:
        _init_metrics()
        from routes.metrics import _get_agent_uptime

        result = _get_agent_uptime()
        assert result["uptime_pct"] == 0.0
        assert result["heartbeat_count"] == 0

    def test_uptime_single_heartbeat(self) -> None:
        _init_metrics()
        from routes.metrics import record_metric, _get_agent_uptime

        record_metric("agent_heartbeat", 1.0)
        result = _get_agent_uptime()
        assert result["uptime_pct"] == 100.0
        assert result["heartbeat_count"] == 1
