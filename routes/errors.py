"""Error tracking endpoints for centralized error collection."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from auth import verify_secret
from db import get_conn

router = APIRouter(tags=["errors"])


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def init_errors_table() -> None:
    """Create the errors table and indexes if they do not exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                component TEXT NOT NULL,
                error TEXT NOT NULL,
                traceback TEXT,
                context TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_errors_channel_time
                ON errors(channel, created_at DESC);
        """)


def store_error(
    channel: str,
    component: str,
    error: str,
    traceback_str: str | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """Store an error report and return its ID."""
    now = time.time()
    context_json = json.dumps(context or {})
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO errors (channel, component, error, traceback, context, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel, component, error, traceback_str, context_json, now),
        )
        return cursor.lastrowid  # type: ignore[return-value]


def get_errors(
    channel: str,
    limit: int = 50,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """Get recent errors for a channel."""
    with get_conn() as conn:
        if since is not None:
            rows = conn.execute(
                """SELECT id, channel, component, error, traceback, context, created_at
                   FROM errors
                   WHERE channel = ? AND created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (channel, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, channel, component, error, traceback, context, created_at
                   FROM errors
                   WHERE channel = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (channel, limit),
            ).fetchall()

    return [
        {
            "id": row["id"],
            "channel": row["channel"],
            "component": row["component"],
            "error": row["error"],
            "traceback": row["traceback"],
            "context": json.loads(row["context"]) if row["context"] else {},
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_recent_errors(limit: int = 10) -> list[dict[str, Any]]:
    """Get recent errors across all channels (for dashboard display)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, channel, component, error, traceback, context, created_at
               FROM errors
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    return [
        {
            "id": row["id"],
            "channel": row["channel"],
            "component": row["component"],
            "error": row["error"],
            "traceback": row["traceback"],
            "context": json.loads(row["context"]) if row["context"] else {},
            "created_at": row["created_at"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ErrorReport(BaseModel):
    """Incoming error report from an agent."""

    component: str
    error: str
    traceback: str | None = None
    context: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/{channel}/errors")
async def report_error(
    channel: str,
    body: ErrorReport,
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    """Store an error report from an agent."""
    verify_secret(secret)

    error_id = store_error(
        channel=channel,
        component=body.component,
        error=body.error,
        traceback_str=body.traceback,
        context=body.context,
    )

    return {"id": error_id, "stored": True}


@router.get("/api/{channel}/errors")
async def list_errors(
    channel: str,
    secret: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    since: float | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List recent errors for a channel."""
    verify_secret(secret)
    return get_errors(channel, limit=limit, since=since)
