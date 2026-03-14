"""Pydantic models for the Voice SDLC relay server."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class PromptRequest(BaseModel):
    """Incoming voice/text prompt from the mobile client."""

    transcript: str = Field(..., min_length=1, max_length=10000)
    repo: Optional[str] = None
    branch: Optional[str] = None
    environment: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    inputType: str = Field(default="voice", pattern="^(voice|text)$")


class PromptResponse(BaseModel):
    """Response after submitting a prompt."""

    id: str
    delivered: bool
    queued: bool
    agentOnline: bool
    message: str


class PlanActionRequest(BaseModel):
    """Action on a plan from the mobile client."""

    plan_id: str = Field(..., min_length=1)
    action: str = Field(..., pattern="^(approve|reject|refine|approve_change|reject_change|cancel)$")
    change_index: Optional[int] = Field(default=None, ge=0)
    refinement: Optional[str] = Field(default=None, max_length=5000)
    rejectionReason: Optional[str] = Field(default=None, max_length=2000)


class PlanActionResponse(BaseModel):
    """Response after submitting a plan action."""

    delivered: bool
    action: str


class StatusResponse(BaseModel):
    """Channel status information."""

    agent_online: bool
    last_heartbeat: Optional[str] = None
    pending_prompts: int
    active_plan: Optional[dict[str, Any]] = None
    channel: str
    relay_uptime_s: float
