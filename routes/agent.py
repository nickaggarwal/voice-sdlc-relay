"""WebSocket endpoint for coding agent connections."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from db import get_undelivered, mark_delivered, store_message, upsert_plan

router = APIRouter(tags=["agent"])

logger = logging.getLogger(__name__)


def _get_state() -> tuple[dict, dict, dict]:
    """Import shared state from main module."""
    import main

    return main.agent_connections, main.mobile_subscribers, main.agent_heartbeats


async def _push_to_mobile(channel: str, event_type: str, data: dict[str, Any]) -> None:
    """Push an event to all SSE subscribers on a channel."""
    _, subscribers, _ = _get_state()
    queues = subscribers.get(channel, [])
    dead_queues: list[int] = []
    event = {"event": event_type, "data": data}
    for i, q in enumerate(queues):
        try:
            q.put_nowait(event)
        except Exception:
            dead_queues.append(i)
    for i in reversed(dead_queues):
        queues.pop(i)


async def _deliver_queued(channel: str, ws: WebSocket) -> None:
    """Deliver any queued mobile-to-agent messages."""
    messages = get_undelivered(channel, "mobile_to_agent")
    delivered_ids = []
    for msg in messages:
        try:
            await ws.send_json(msg["payload"])
            delivered_ids.append(msg["id"])
        except Exception:
            break
    if delivered_ids:
        mark_delivered(delivered_ids)
        logger.info(f"Delivered {len(delivered_ids)} queued messages to agent on channel={channel}")


@router.websocket("/ws/{channel}")
async def agent_websocket(
    websocket: WebSocket,
    channel: str,
    secret: str | None = Query(default=None),
) -> None:
    """WebSocket endpoint for agent connections."""
    import hmac

    # Validate secret before accepting
    expected = os.environ.get("RELAY_SECRET", "change-me-in-production")
    if secret is None or not hmac.compare_digest(secret, expected):
        await websocket.close(code=4001, reason="Invalid secret")
        return

    await websocket.accept()

    connections, subscribers, heartbeats = _get_state()

    # Last-writer-wins: close existing agent on same channel
    existing = connections.get(channel)
    if existing is not None:
        try:
            await existing.close(code=4000, reason="Replaced by new connection")
        except Exception:
            pass
        logger.info(f"Replaced existing agent on channel={channel}")

    # Register this agent
    connections[channel] = websocket
    now_str = datetime.now(timezone.utc).isoformat()
    heartbeats[channel] = now_str

    logger.info(f"Agent connected: channel={channel}")

    # Notify mobile subscribers that agent is online
    await _push_to_mobile(channel, "agent_status", {"online": True, "channel": channel})

    # Deliver any queued messages
    await _deliver_queued(channel, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from agent on channel={channel}")
                continue

            msg_type = message.get("type", "unknown")

            # Handle heartbeat
            if msg_type == "heartbeat":
                now_str = datetime.now(timezone.utc).isoformat()
                heartbeats[channel] = now_str
                # Respond with ack, skip storage
                try:
                    await websocket.send_json({
                        "type": "heartbeat_ack",
                        "timestamp": now_str,
                    })
                except Exception:
                    break
                continue

            # For plan_ready/plan_updated: upsert plan in DB then push to mobile
            if msg_type in ("plan_ready", "plan_updated"):
                plan_data = message.get("plan", message)
                plan_id = plan_data.get("plan_id", plan_data.get("id", "unknown"))
                status = plan_data.get("status", "pending")
                change_type = plan_data.get("change_type")
                summary = plan_data.get("summary")
                upsert_plan(
                    channel=channel,
                    plan_id=plan_id,
                    status=status,
                    change_type=change_type,
                    summary=summary,
                    payload=plan_data,
                )
                logger.info(f"Plan upserted: channel={channel} plan_id={plan_id} type={msg_type}")

            # Store all non-heartbeat messages in DB
            store_message(channel, "agent_to_mobile", msg_type, message)

            # Push to all mobile SSE subscribers
            await _push_to_mobile(channel, msg_type, message)

    except WebSocketDisconnect:
        logger.info(f"Agent disconnected: channel={channel}")
    except Exception as exc:
        logger.error(f"Agent WebSocket error on channel={channel}: {exc}")
    finally:
        # Remove from connections only if this is still the current connection
        if connections.get(channel) is websocket:
            connections.pop(channel, None)
            heartbeats.pop(channel, None)

        # Notify mobile subscribers that agent is offline
        await _push_to_mobile(channel, "agent_status", {"online": False, "channel": channel})
