"""Metrics recording, querying, and dashboard endpoints."""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from db import get_conn

router = APIRouter(tags=["metrics"])


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def init_metrics_table() -> None:
    """Create the metrics table and indexes if they do not exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                tags TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_name_time
                ON metrics(name, created_at DESC);
        """)


def record_metric(name: str, value: float, tags: dict[str, str] | None = None) -> None:
    """Insert a metric data point into the database."""
    tags_json = json.dumps(tags or {})
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO metrics (name, value, tags, created_at) VALUES (?, ?, ?, ?)",
            (name, value, tags_json, now),
        )


def get_metrics(
    name: str,
    since: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query metric data points by name, optionally filtered by time."""
    with get_conn() as conn:
        if since is not None:
            rows = conn.execute(
                """SELECT id, name, value, tags, created_at
                   FROM metrics
                   WHERE name = ? AND created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (name, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, name, value, tags, created_at
                   FROM metrics
                   WHERE name = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (name, limit),
            ).fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "value": row["value"],
            "tags": json.loads(row["tags"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_metric_summary(name: str, since: float | None = None) -> dict[str, Any]:
    """Return statistical summary for a named metric.

    Returns:
        { count, min, max, avg, p50, p95 }
    """
    with get_conn() as conn:
        if since is not None:
            rows = conn.execute(
                "SELECT value FROM metrics WHERE name = ? AND created_at >= ? ORDER BY value",
                (name, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT value FROM metrics WHERE name = ? ORDER BY value",
                (name,),
            ).fetchall()

    values = [row["value"] for row in rows]

    if not values:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0}

    sorted_vals = sorted(values)
    count = len(sorted_vals)

    return {
        "count": count,
        "min": round(sorted_vals[0], 3),
        "max": round(sorted_vals[-1], 3),
        "avg": round(statistics.mean(sorted_vals), 3),
        "p50": round(sorted_vals[int(count * 0.5)], 3) if count > 0 else 0,
        "p95": round(sorted_vals[min(int(count * 0.95), count - 1)], 3) if count > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Helper: aggregate pass/fail rate metrics
# ---------------------------------------------------------------------------


def _get_rate_summary(pass_metric: str, fail_metric: str, since: float | None = None) -> dict[str, Any]:
    """Calculate pass rate from separate pass/fail metric counters."""
    pass_data = get_metrics(pass_metric, since=since, limit=100000)
    fail_data = get_metrics(fail_metric, since=since, limit=100000)

    passed = len(pass_data)
    failed = len(fail_data)
    total = passed + failed
    rate = round(passed / total, 4) if total > 0 else 0.0

    return {"total": total, "passed": passed, "failed": failed, "rate": rate}


def _get_deploy_rate_summary(since: float | None = None) -> dict[str, Any]:
    """Calculate deploy success rate from success/failure counters."""
    success_data = get_metrics("deploy_success", since=since, limit=100000)
    failure_data = get_metrics("deploy_failure", since=since, limit=100000)

    succeeded = len(success_data)
    failed = len(failure_data)
    total = succeeded + failed
    rate = round(succeeded / total, 4) if total > 0 else 0.0

    return {"total": total, "succeeded": succeeded, "failed": failed, "rate": rate}


def _get_autofix_rate_summary(since: float | None = None) -> dict[str, Any]:
    """Calculate auto-fix success rate."""
    success_data = get_metrics("autofix_success", since=since, limit=100000)
    failure_data = get_metrics("autofix_failure", since=since, limit=100000)

    succeeded = len(success_data)
    failed = len(failure_data)
    total = succeeded + failed
    rate = round(succeeded / total, 4) if total > 0 else 0.0

    return {"total": total, "succeeded": succeeded, "failed": failed, "rate": rate}


def _get_validation_by_change_type(since: float | None = None) -> dict[str, dict[str, Any]]:
    """Get average validation duration grouped by changeType tag."""
    all_data = get_metrics("validation_duration", since=since, limit=100000)
    by_type: dict[str, list[float]] = {}

    for point in all_data:
        ct = point.get("tags", {}).get("changeType", "unknown")
        by_type.setdefault(ct, []).append(point["value"])

    result: dict[str, dict[str, Any]] = {}
    for ct, vals in by_type.items():
        result[ct] = {
            "count": len(vals),
            "avg": round(statistics.mean(vals), 3) if vals else 0,
            "p95": round(sorted(vals)[min(int(len(vals) * 0.95), len(vals) - 1)], 3) if vals else 0,
        }
    return result


def _get_queue_depth() -> int:
    """Get the number of undelivered messages in the queue."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM messages WHERE delivered = 0").fetchone()
    return row[0] if row else 0


def _get_agent_uptime(since: float | None = None) -> dict[str, Any]:
    """Estimate agent uptime from heartbeat metric records.

    Uses the heartbeat intervals to calculate what percentage of time
    agents were connected. A heartbeat every 30s means the agent was
    connected during that interval.
    """
    heartbeats = get_metrics("agent_heartbeat", since=since, limit=100000)

    if len(heartbeats) < 2:
        return {"uptime_pct": 100.0 if heartbeats else 0.0, "heartbeat_count": len(heartbeats)}

    # Sort by timestamp ascending
    sorted_hb = sorted(heartbeats, key=lambda x: x["created_at"])
    total_span = sorted_hb[-1]["created_at"] - sorted_hb[0]["created_at"]

    if total_span <= 0:
        return {"uptime_pct": 100.0, "heartbeat_count": len(heartbeats)}

    # Each heartbeat covers up to 35s (heartbeat interval + grace)
    coverage = 0.0
    for i in range(1, len(sorted_hb)):
        gap = sorted_hb[i]["created_at"] - sorted_hb[i - 1]["created_at"]
        # If the gap is <= 35s, the agent was connected for the whole interval
        if gap <= 35.0:
            coverage += gap
        else:
            # Agent was connected for some portion then disconnected
            coverage += 35.0

    uptime_pct = round(min(coverage / total_span * 100, 100.0), 2)
    return {"uptime_pct": uptime_pct, "heartbeat_count": len(heartbeats)}


# ---------------------------------------------------------------------------
# HTML bar chart helper
# ---------------------------------------------------------------------------


def _bar_html(label: str, value: float, max_value: float, unit: str = "", color: str = "#58a6ff") -> str:
    """Generate HTML for a single bar in a bar chart."""
    width_pct = min(value / max_value * 100, 100) if max_value > 0 else 0
    display_value = f"{value:.1f}{unit}" if isinstance(value, float) else f"{value}{unit}"
    return f"""<div style="margin-bottom:6px;">
        <div style="display:flex;align-items:center;gap:8px;">
            <span style="width:140px;font-size:0.8rem;color:#8b949e;text-align:right;">{label}</span>
            <div style="flex:1;background:#21262d;border-radius:4px;height:20px;overflow:hidden;">
                <div style="width:{width_pct:.1f}%;background:{color};height:100%;border-radius:4px;min-width:2px;"></div>
            </div>
            <span style="width:80px;font-size:0.8rem;color:#c9d1d9;">{display_value}</span>
        </div>
    </div>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_dashboard() -> HTMLResponse:
    """HTML metrics dashboard page with dark theme."""
    one_day_ago = time.time() - 86400

    # Gather all metric summaries
    prompt_to_plan = get_metric_summary("prompt_to_plan_latency", since=one_day_ago)
    plan_to_deploy = get_metric_summary("plan_to_deploy_latency", since=one_day_ago)
    validation_rate = _get_rate_summary("validation_pass", "validation_fail", since=one_day_ago)
    autofix_rate = _get_autofix_rate_summary(since=one_day_ago)
    deploy_rate = _get_deploy_rate_summary(since=one_day_ago)
    validation_by_type = _get_validation_by_change_type(since=one_day_ago)
    queue_depth = _get_queue_depth()
    agent_uptime = _get_agent_uptime(since=one_day_ago)

    # Build validation by changeType bars
    val_by_type_bars = ""
    if validation_by_type:
        max_avg = max((v["avg"] for v in validation_by_type.values()), default=1)
        for ct, data in sorted(validation_by_type.items()):
            val_by_type_bars += _bar_html(ct, data["avg"], max_avg, "ms", "#d2a8ff")
    else:
        val_by_type_bars = '<span class="empty">No validation data</span>'

    # Build latency bars
    latency_bars = ""
    latency_items = [
        ("Prompt-to-Plan avg", prompt_to_plan["avg"], "ms"),
        ("Prompt-to-Plan p95", prompt_to_plan["p95"], "ms"),
        ("Plan-to-Deploy avg", plan_to_deploy["avg"], "ms"),
        ("Plan-to-Deploy p95", plan_to_deploy["p95"], "ms"),
    ]
    max_latency = max((item[1] for item in latency_items), default=1)
    if max_latency == 0:
        max_latency = 1
    for label, val, unit in latency_items:
        latency_bars += _bar_html(label, val, max_latency, unit)

    # Recent errors section
    try:
        from routes.errors import get_recent_errors
        recent_errors = get_recent_errors(limit=10)
    except Exception:
        recent_errors = []

    error_rows = ""
    for err in recent_errors:
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(err["created_at"]).strftime("%H:%M:%S")
        error_rows += f"""<tr>
            <td style="color:#8b949e;font-size:0.75rem;">{ts}</td>
            <td><span class="channel-badge">{err['channel']}</span></td>
            <td style="color:#d2a8ff;">{err['component']}</td>
            <td style="color:#f85149;">{err['error'][:80]}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>Metrics - Voice SDLC Relay</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
            background: #0d1117;
            color: #c9d1d9;
            padding: 2rem;
        }}
        h1 {{
            color: #58a6ff;
            margin-bottom: 0.5rem;
            font-size: 1.5rem;
        }}
        .subtitle {{
            color: #8b949e;
            margin-bottom: 2rem;
            font-size: 0.9rem;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .card {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 1.25rem;
        }}
        .card .label {{
            color: #8b949e;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .card .value {{
            color: #f0f6fc;
            font-size: 1.75rem;
            font-weight: bold;
            margin-top: 0.25rem;
        }}
        .card .value.green {{ color: #3fb950; }}
        .card .value.yellow {{ color: #d29922; }}
        .card .value.red {{ color: #f85149; }}
        .section {{
            margin-bottom: 2rem;
        }}
        .section h2 {{
            color: #58a6ff;
            font-size: 1.1rem;
            margin-bottom: 0.75rem;
            border-bottom: 1px solid #30363d;
            padding-bottom: 0.5rem;
        }}
        .chart-container {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 1.25rem;
            margin-bottom: 1rem;
        }}
        .channel-badge {{
            background: #1f6feb33;
            color: #58a6ff;
            padding: 0.15rem 0.5rem;
            border-radius: 8px;
            font-size: 0.75rem;
            border: 1px solid #1f6feb55;
        }}
        .empty {{
            color: #484f58;
            font-style: italic;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        table td {{
            padding: 0.4rem 0.6rem;
            border-bottom: 1px solid #21262d;
            font-size: 0.85rem;
        }}
        .nav {{
            margin-bottom: 1.5rem;
        }}
        .nav a {{
            color: #58a6ff;
            text-decoration: none;
            margin-right: 1rem;
            font-size: 0.9rem;
        }}
        .nav a:hover {{
            text-decoration: underline;
        }}
        .footer {{
            margin-top: 2rem;
            color: #484f58;
            font-size: 0.8rem;
            border-top: 1px solid #21262d;
            padding-top: 1rem;
        }}
    </style>
</head>
<body>
    <h1>Voice SDLC Metrics</h1>
    <p class="subtitle">Observability dashboard - auto-refreshes every 30 seconds</p>
    <div class="nav">
        <a href="/">Dashboard</a>
        <a href="/metrics">Metrics</a>
        <a href="/api/metrics">JSON API</a>
    </div>

    <div class="grid">
        <div class="card">
            <div class="label">Validation Pass Rate</div>
            <div class="value {'green' if validation_rate['rate'] >= 0.8 else 'yellow' if validation_rate['rate'] >= 0.5 else 'red'}">{validation_rate['rate'] * 100:.1f}%</div>
            <div class="label">{validation_rate['passed']}/{validation_rate['total']} passed</div>
        </div>
        <div class="card">
            <div class="label">Auto-Fix Success Rate</div>
            <div class="value {'green' if autofix_rate['rate'] >= 0.7 else 'yellow' if autofix_rate['rate'] >= 0.4 else 'red'}">{autofix_rate['rate'] * 100:.1f}%</div>
            <div class="label">{autofix_rate['succeeded']}/{autofix_rate['total']} fixed</div>
        </div>
        <div class="card">
            <div class="label">Deploy Success Rate</div>
            <div class="value {'green' if deploy_rate['rate'] >= 0.9 else 'yellow' if deploy_rate['rate'] >= 0.7 else 'red'}">{deploy_rate['rate'] * 100:.1f}%</div>
            <div class="label">{deploy_rate['succeeded']}/{deploy_rate['total']} deployed</div>
        </div>
        <div class="card">
            <div class="label">Message Queue Depth</div>
            <div class="value {'green' if queue_depth < 10 else 'yellow' if queue_depth < 50 else 'red'}">{queue_depth}</div>
            <div class="label">pending messages</div>
        </div>
        <div class="card">
            <div class="label">Agent Uptime</div>
            <div class="value {'green' if agent_uptime['uptime_pct'] >= 95 else 'yellow' if agent_uptime['uptime_pct'] >= 80 else 'red'}">{agent_uptime['uptime_pct']:.1f}%</div>
            <div class="label">{agent_uptime['heartbeat_count']} heartbeats</div>
        </div>
        <div class="card">
            <div class="label">Prompt-to-Plan Avg</div>
            <div class="value">{prompt_to_plan['avg']:.0f}<span style="font-size:0.9rem;color:#8b949e;">ms</span></div>
            <div class="label">p95: {prompt_to_plan['p95']:.0f}ms ({prompt_to_plan['count']} samples)</div>
        </div>
    </div>

    <div class="section">
        <h2>Latency Breakdown (last 24h)</h2>
        <div class="chart-container">
            {latency_bars if prompt_to_plan['count'] > 0 or plan_to_deploy['count'] > 0 else '<span class="empty">No latency data yet</span>'}
        </div>
    </div>

    <div class="section">
        <h2>Avg Validation Duration by Change Type (last 24h)</h2>
        <div class="chart-container">
            {val_by_type_bars}
        </div>
    </div>

    <div class="section">
        <h2>Recent Errors</h2>
        <div class="chart-container">
            {'<table>' + error_rows + '</table>' if error_rows else '<span class="empty">No recent errors</span>'}
        </div>
    </div>

    <div class="footer">
        Voice SDLC Relay v1.0.0 | Metrics page auto-refreshes every 30s |
        <a href="/api/metrics" style="color:#58a6ff;">JSON API</a>
    </div>
</body>
</html>"""

    return HTMLResponse(content=html)


@router.get("/api/metrics")
async def metrics_json(
    since_hours: float = Query(default=24, description="Look back this many hours"),
) -> dict[str, Any]:
    """JSON API returning all metric summaries for custom dashboards."""
    since = time.time() - (since_hours * 3600)

    return {
        "prompt_to_plan_latency": get_metric_summary("prompt_to_plan_latency", since=since),
        "plan_to_deploy_latency": get_metric_summary("plan_to_deploy_latency", since=since),
        "request_duration": get_metric_summary("request_duration", since=since),
        "validation_pass_rate": _get_rate_summary("validation_pass", "validation_fail", since=since),
        "autofix_success_rate": _get_autofix_rate_summary(since=since),
        "deploy_success_rate": _get_deploy_rate_summary(since=since),
        "validation_duration_by_type": _get_validation_by_change_type(since=since),
        "queue_depth": _get_queue_depth(),
        "agent_uptime": _get_agent_uptime(since=since),
    }
