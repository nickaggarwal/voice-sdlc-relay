"""REST endpoints for mobile clients."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from auth import verify_secret
from db import get_plan, get_plans, get_undelivered, mark_delivered, store_message, upsert_plan
from models import (
    PlanActionRequest,
    PlanActionResponse,
    PromptRequest,
    PromptResponse,
    StatusResponse,
)

router = APIRouter(prefix="/api/{channel}", tags=["mobile"])

logger = logging.getLogger(__name__)


def _get_state() -> tuple[dict, dict, dict, float]:
    """Import shared state from main module."""
    import main

    return (
        main.agent_connections,
        main.mobile_subscribers,
        main.agent_heartbeats,
        main.start_time,
    )


async def _push_to_mobile(channel: str, event_type: str, data: dict[str, Any]) -> None:
    """Push an event to all SSE subscribers on a channel."""
    _, subscribers, _, _ = _get_state()
    queues = subscribers.get(channel, [])
    event = {"event": event_type, "data": data}
    dead_queues: list[int] = []
    for i, q in enumerate(queues):
        try:
            q.put_nowait(event)
        except Exception:
            dead_queues.append(i)
    # Remove dead queues in reverse order
    for i in reversed(dead_queues):
        queues.pop(i)


async def _send_to_agent(channel: str, message: dict[str, Any]) -> bool:
    """Try to send a message to the connected agent. Returns True if delivered."""
    connections, _, _, _ = _get_state()
    ws = connections.get(channel)
    if ws is None:
        return False
    try:
        await ws.send_json(message)
        return True
    except Exception:
        # Agent disconnected
        connections.pop(channel, None)
        return False


@router.post("/prompt", response_model=PromptResponse)
async def submit_prompt(
    channel: str,
    body: PromptRequest,
    secret: str | None = Query(default=None),
) -> PromptResponse:
    """Submit a voice/text prompt to the coding agent."""
    verify_secret(secret)

    msg_id = str(uuid.uuid4())
    relay_message = {
        "type": "prompt",
        "id": msg_id,
        "channel": channel,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transcript": body.transcript,
        "repo": body.repo,
        "branch": body.branch,
        "environment": body.environment,
        "duration_ms": body.duration_ms,
        "confidence": body.confidence,
        "inputType": body.inputType,
    }

    # Store in DB
    store_message(channel, "mobile_to_agent", "prompt", relay_message)

    # Try to deliver to agent
    delivered = await _send_to_agent(channel, relay_message)
    if delivered:
        mark_delivered([msg_id])

    connections, _, _, _ = _get_state()
    agent_online = channel in connections

    return PromptResponse(
        id=msg_id,
        delivered=delivered,
        queued=not delivered,
        agentOnline=agent_online,
        message="Prompt delivered to agent" if delivered else "Prompt queued for agent",
    )


@router.post("/plan-action", response_model=PlanActionResponse)
async def submit_plan_action(
    channel: str,
    body: PlanActionRequest,
    secret: str | None = Query(default=None),
) -> PlanActionResponse:
    """Submit a plan action (approve, reject, refine, etc.)."""
    verify_secret(secret)

    msg_id = str(uuid.uuid4())
    relay_message: dict[str, Any] = {
        "type": "plan_action",
        "id": msg_id,
        "channel": channel,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan_id": body.plan_id,
        "action": body.action,
    }
    if body.change_index is not None:
        relay_message["change_index"] = body.change_index
    if body.refinement is not None:
        relay_message["refinement"] = body.refinement
    if body.rejectionReason is not None:
        relay_message["rejectionReason"] = body.rejectionReason

    # Store in DB
    store_message(channel, "mobile_to_agent", "plan_action", relay_message)

    # Update plan status based on action
    action_to_status = {
        "approve": "approved",
        "reject": "rejected",
        "refine": "refining",
        "cancel": "cancelled",
        "approve_change": "partial_approved",
        "reject_change": "partial_rejected",
    }
    new_status = action_to_status.get(body.action, body.action)
    upsert_plan(channel, body.plan_id, new_status)

    # Try to deliver to agent
    delivered = await _send_to_agent(channel, relay_message)
    if delivered:
        mark_delivered([msg_id])

    return PlanActionResponse(delivered=delivered, action=body.action)


@router.get("/plans")
async def list_plans(
    channel: str,
    secret: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    status: str = Query(default="all"),
    since: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List plans for a channel."""
    verify_secret(secret)
    return get_plans(channel, limit=limit, status_filter=status, since=since)


@router.get("/plans/{plan_id}")
async def get_single_plan(
    channel: str,
    plan_id: str,
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    """Get a single plan by ID."""
    verify_secret(secret)
    plan = get_plan(channel, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.delete("/plans/{plan_id}")
async def cancel_plan(
    channel: str,
    plan_id: str,
    secret: str | None = Query(default=None),
) -> dict[str, Any]:
    """Cancel a plan."""
    verify_secret(secret)
    plan = get_plan(channel, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    upsert_plan(channel, plan_id, "cancelled")

    # Send cancellation to agent
    cancel_msg: dict[str, Any] = {
        "type": "plan_action",
        "id": str(uuid.uuid4()),
        "channel": channel,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "action": "cancel",
    }
    store_message(channel, "mobile_to_agent", "plan_action", cancel_msg)
    delivered = await _send_to_agent(channel, cancel_msg)

    # Notify mobile subscribers
    await _push_to_mobile(channel, "plan_updated", {"plan_id": plan_id, "status": "cancelled"})

    return {"plan_id": plan_id, "status": "cancelled", "delivered": delivered}


@router.get("/status", response_model=StatusResponse)
async def get_status(
    channel: str,
    secret: str | None = Query(default=None),
) -> StatusResponse:
    """Get the status of a channel."""
    verify_secret(secret)

    connections, _, heartbeats, start = _get_state()
    agent_online = channel in connections

    # Count pending (undelivered) prompts going to agent
    pending = get_undelivered(channel, "mobile_to_agent")
    pending_prompts = sum(1 for m in pending if m["msg_type"] == "prompt")

    # Get most recent active plan
    active_plans = get_plans(channel, limit=1, status_filter="pending")
    active_plan = active_plans[0] if active_plans else None

    last_hb = heartbeats.get(channel)

    uptime = time.time() - start if start > 0 else 0.0

    return StatusResponse(
        agent_online=agent_online,
        last_heartbeat=last_hb,
        pending_prompts=pending_prompts,
        active_plan=active_plan,
        channel=channel,
        relay_uptime_s=round(uptime, 2),
    )
