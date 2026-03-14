"""Dashboard and health check endpoints."""

from __future__ import annotations

import os
import resource
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from db import get_stats

router = APIRouter(tags=["dashboard"])


def _get_state() -> tuple[dict, dict, float]:
    """Import shared state from main module."""
    import main

    return main.agent_connections, main.mobile_subscribers, main.start_time


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """HTML dashboard with dark theme showing stats, channels, and recent info."""
    connections, subscribers, start = _get_state()
    stats = get_stats()
    uptime_s = time.time() - start if start > 0 else 0
    uptime_h = uptime_s / 3600

    agent_channels = list(connections.keys())
    subscriber_channels = list(subscribers.keys())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voice SDLC Relay</title>
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
        .channel-list {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }}
        .channel-badge {{
            background: #1f6feb33;
            color: #58a6ff;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.85rem;
            border: 1px solid #1f6feb55;
        }}
        .channel-badge.agent {{
            background: #23863633;
            color: #3fb950;
            border-color: #23863655;
        }}
        .empty {{
            color: #484f58;
            font-style: italic;
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
    <h1>Voice SDLC Relay</h1>
    <p class="subtitle">Thin message router between mobile clients and coding agents</p>

    <div class="grid">
        <div class="card">
            <div class="label">Uptime</div>
            <div class="value">{uptime_h:.1f}h</div>
        </div>
        <div class="card">
            <div class="label">Total Messages</div>
            <div class="value">{stats['total_messages']}</div>
        </div>
        <div class="card">
            <div class="label">Undelivered</div>
            <div class="value">{stats['undelivered_messages']}</div>
        </div>
        <div class="card">
            <div class="label">Plans</div>
            <div class="value">{stats['total_plans']}</div>
        </div>
        <div class="card">
            <div class="label">Connected Agents</div>
            <div class="value">{len(agent_channels)}</div>
        </div>
        <div class="card">
            <div class="label">SSE Subscribers</div>
            <div class="value">{sum(len(v) for v in subscribers.values())}</div>
        </div>
    </div>

    <div class="section">
        <h2>Connected Agents</h2>
        <div class="channel-list">
            {''.join(f'<span class="channel-badge agent">{ch}</span>' for ch in agent_channels) if agent_channels else '<span class="empty">No agents connected</span>'}
        </div>
    </div>

    <div class="section">
        <h2>Active Channels</h2>
        <div class="channel-list">
            {''.join(f'<span class="channel-badge">{ch}</span>' for ch in stats["active_channels"]) if stats["active_channels"] else '<span class="empty">No active channels</span>'}
        </div>
    </div>

    <div class="section">
        <h2>SSE Subscriber Channels</h2>
        <div class="channel-list">
            {''.join(f'<span class="channel-badge">{ch}</span>' for ch in subscriber_channels) if subscriber_channels else '<span class="empty">No subscribers</span>'}
        </div>
    </div>

    <div class="footer">
        Voice SDLC Relay v1.0.0 | Refresh page for updated stats
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """JSON health endpoint with uptime, counts, and memory usage."""
    connections, subscribers, start = _get_state()
    stats = get_stats()
    uptime_s = time.time() - start if start > 0 else 0

    # Get memory usage (platform-dependent, best-effort)
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # maxrss is in kilobytes on Linux, bytes on macOS
        memory_mb = usage.ru_maxrss / (1024 * 1024) if os.uname().sysname == "Darwin" else usage.ru_maxrss / 1024
    except Exception:
        memory_mb = 0.0

    return {
        "status": "healthy",
        "version": "1.0.0",
        "uptime_s": round(uptime_s, 2),
        "connected_agents": len(connections),
        "sse_subscribers": sum(len(v) for v in subscribers.values()),
        "total_messages": stats["total_messages"],
        "undelivered_messages": stats["undelivered_messages"],
        "total_plans": stats["total_plans"],
        "active_channels": stats["active_channels"],
        "memory_mb": round(memory_mb, 2),
    }
