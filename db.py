"""SQLite persistence layer with WAL mode for the relay server."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator


DB_PATH = os.environ.get("DB_PATH", "relay.db")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Get a database connection with WAL mode and row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they do not exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                direction TEXT NOT NULL,
                msg_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_messages_channel_direction
                ON messages(channel, direction);
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
                ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_delivered
                ON messages(delivered);

            CREATE TABLE IF NOT EXISTS agents (
                channel TEXT PRIMARY KEY,
                last_heartbeat TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS plans (
                id TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                change_type TEXT,
                summary TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (channel, id)
            );

            CREATE INDEX IF NOT EXISTS idx_plans_channel_status
                ON plans(channel, status);
            CREATE INDEX IF NOT EXISTS idx_plans_updated_at
                ON plans(updated_at);
        """)


def store_message(
    channel: str,
    direction: str,
    msg_type: str,
    payload: dict[str, Any],
) -> str:
    """Store a message and return its UUID."""
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload_json = json.dumps(payload)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO messages (id, channel, direction, msg_type, payload, delivered, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (msg_id, channel, direction, msg_type, payload_json, now),
        )
    return msg_id


def get_undelivered(
    channel: str,
    direction: str,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Get undelivered messages for a channel/direction, optionally since a timestamp."""
    with get_conn() as conn:
        if since:
            rows = conn.execute(
                """SELECT id, channel, direction, msg_type, payload, created_at
                   FROM messages
                   WHERE channel = ? AND direction = ? AND delivered = 0 AND created_at >= ?
                   ORDER BY created_at ASC""",
                (channel, direction, since),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, channel, direction, msg_type, payload, created_at
                   FROM messages
                   WHERE channel = ? AND direction = ? AND delivered = 0
                   ORDER BY created_at ASC""",
                (channel, direction),
            ).fetchall()
    return [
        {
            "id": row["id"],
            "channel": row["channel"],
            "direction": row["direction"],
            "msg_type": row["msg_type"],
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def mark_delivered(msg_ids: list[str]) -> None:
    """Mark messages as delivered by their IDs."""
    if not msg_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in msg_ids)
        conn.execute(
            f"UPDATE messages SET delivered = 1 WHERE id IN ({placeholders})",
            msg_ids,
        )


def upsert_plan(
    channel: str,
    plan_id: str,
    status: str,
    change_type: str | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert or update a plan record."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload_json = json.dumps(payload or {})
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO plans (id, channel, status, change_type, summary, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel, id) DO UPDATE SET
                   status = excluded.status,
                   change_type = COALESCE(excluded.change_type, plans.change_type),
                   summary = COALESCE(excluded.summary, plans.summary),
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (plan_id, channel, status, change_type, summary, payload_json, now, now),
        )


def get_plans(
    channel: str,
    limit: int = 20,
    status_filter: str = "all",
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Get plans for a channel with optional filtering."""
    conditions = ["channel = ?"]
    params: list[Any] = [channel]

    if status_filter and status_filter != "all":
        conditions.append("status = ?")
        params.append(status_filter)

    if since:
        conditions.append("updated_at >= ?")
        params.append(since)

    where = " AND ".join(conditions)
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, channel, status, change_type, summary, payload, created_at, updated_at
                FROM plans
                WHERE {where}
                ORDER BY updated_at DESC
                LIMIT ?""",
            params,
        ).fetchall()

    return [
        {
            "id": row["id"],
            "channel": row["channel"],
            "status": row["status"],
            "change_type": row["change_type"],
            "summary": row["summary"],
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_plan(channel: str, plan_id: str) -> dict[str, Any] | None:
    """Get a single plan by channel and plan_id."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, channel, status, change_type, summary, payload, created_at, updated_at
               FROM plans
               WHERE channel = ? AND id = ?""",
            (channel, plan_id),
        ).fetchone()

    if row is None:
        return None

    return {
        "id": row["id"],
        "channel": row["channel"],
        "status": row["status"],
        "change_type": row["change_type"],
        "summary": row["summary"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def cleanup_old(max_age_hours: int = 24) -> int:
    """Delete messages and plans older than max_age_hours. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    total = 0
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE created_at < ?", (cutoff,)
        )
        total += cursor.rowcount
        cursor = conn.execute(
            "DELETE FROM plans WHERE updated_at < ?", (cutoff,)
        )
        total += cursor.rowcount
    return total


def get_stats() -> dict[str, Any]:
    """Get database statistics."""
    with get_conn() as conn:
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        undelivered_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE delivered = 0"
        ).fetchone()[0]
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        channels = conn.execute(
            "SELECT DISTINCT channel FROM messages UNION SELECT DISTINCT channel FROM plans"
        ).fetchall()

    return {
        "total_messages": msg_count,
        "undelivered_messages": undelivered_count,
        "total_plans": plan_count,
        "connected_agents": agent_count,
        "active_channels": [row[0] for row in channels],
    }
