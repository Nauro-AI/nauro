"""Nauro MCP server — local FastAPI HTTP server exposing context tools.

Runs on localhost:7432 (configurable). No auth in v1 — local only.

MCP tools:
  - nauro.get_context(level)          → Return project context at L0/L1/L2 detail
  - nauro.propose_decision()          → Record a decision (single-call commit)
  - nauro.check_decision()            → Check for conflicts without writing
  - nauro.flag_question()             → Flag an open question for human review
  - nauro.update_state()              → Update current project state
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from nauro.mcp.tools import (
    tool_check_decision,
    tool_flag_question,
    tool_get_context,
    tool_propose_decision,
    tool_update_state,
)
from nauro.store.resolution import (
    MultipleProjectsError,
    NoProjectError,
    ProjectIdMismatchError,
    ProjectNotFoundError,
    StoreMissingError,
    resolve_store,
)

logger = logging.getLogger("nauro.mcp")

app = FastAPI(title="Nauro MCP Server", version="0.2.0")


@app.middleware("http")
async def _telemetry_transport_middleware(request, call_next):
    """Set transport='http' for this request.

    The mcp.tool_called decorator reads transport from a ContextVar; each
    FastAPI request runs in its own asyncio Task, so the value is
    request-scoped and cannot leak to other transports.
    """
    from nauro.telemetry.transport import set_transport

    set_transport("http")
    return await call_next(request)


# --- Request models ---


class ContextRequest(BaseModel):
    project_id: str | None = None
    cwd: str | None = None
    level: int = 0  # 0, 1, or 2


class ProposeDecisionRequest(BaseModel):
    project_id: str
    title: str
    rationale: str
    operation: str = "add"
    affected_decision_id: str | None = None
    rejected: list[dict] | None = None
    confidence: str | None = None
    decision_type: str | None = None
    reversibility: str | None = None
    files_affected: list[str] | None = None


class CheckDecisionRequest(BaseModel):
    project_id: str | None = None
    cwd: str | None = None
    proposed_approach: str
    context: str | None = None


class FlagQuestionRequest(BaseModel):
    project_id: str
    question: str
    context: str | None = None


class UpdateStateRequest(BaseModel):
    project_id: str
    delta: str


# --- Helpers ---


def _resolve_store(project_id: str | None, cwd: str | None) -> Path:
    """Resolve project id/name to a store path, raising HTTP errors on failure.

    Delegates to :func:`nauro.store.resolution.resolve_store` and translates
    each typed :class:`StoreResolutionError` subclass into an appropriate
    HTTP status code so existing callers keep their 4xx contracts.
    ``NoProjectError`` / ``ProjectNotFoundError`` / ``StoreMissingError`` →
    404 (resource not present); ``ProjectIdMismatchError`` /
    ``MultipleProjectsError`` → 400 (caller supplied an ambiguous or
    contradictory handle).
    """
    try:
        return resolve_store(project_id, cwd)
    except (NoProjectError, ProjectNotFoundError, StoreMissingError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ProjectIdMismatchError, MultipleProjectsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- MCP tool endpoints ---


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/context")
async def get_context(req: ContextRequest) -> dict:
    """Return project context at the requested detail level (L0/L1/L2)."""
    store_path = _resolve_store(req.project_id, req.cwd)
    try:
        payload = tool_get_context(store_path, req.level)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Kernel-side rejections (invalid level) surface as an ``error`` field
    # on the envelope; preserve the pre-cutover 400 status by re-raising.
    error = payload.get("error") if isinstance(payload, dict) else None
    if error and error.get("kind") == "rejected":
        raise HTTPException(status_code=400, detail=error.get("reason", "Invalid level"))
    return {"level": req.level, "content": payload}


@app.post("/propose_decision")
async def propose_decision(req: ProposeDecisionRequest) -> dict:
    """Propose a new decision with validation. Returns validation results."""
    store_path = _resolve_store(req.project_id, None)
    return tool_propose_decision(
        store_path,
        title=req.title,
        rationale=req.rationale,
        operation=req.operation,
        affected_decision_id=req.affected_decision_id,
        rejected=req.rejected,
        confidence=req.confidence,
        decision_type=req.decision_type,
        reversibility=req.reversibility,
        files_affected=req.files_affected,
    )


@app.post("/check_decision")
async def check_decision_endpoint(req: CheckDecisionRequest) -> dict:
    """Check for conflicts with existing decisions without writing."""
    store_path = _resolve_store(req.project_id, req.cwd)
    return tool_check_decision(store_path, req.proposed_approach, req.context)


# --- Legacy endpoints (kept for backwards compat, redirect to propose) ---


@app.post("/log_decision")
async def log_decision_legacy(req: ProposeDecisionRequest) -> dict:
    """Legacy endpoint — redirects to propose_decision."""
    return await propose_decision(req)


@app.post("/flag_question")
async def flag_question(req: FlagQuestionRequest) -> dict:
    """Flag an open question for human review. Checks against existing decisions."""
    store_path = _resolve_store(req.project_id, None)
    return tool_flag_question(store_path, req.question, req.context)


@app.post("/update_state")
async def update_state_endpoint(req: UpdateStateRequest) -> dict:
    """Update current project state. Checks for contradictions."""
    store_path = _resolve_store(req.project_id, None)
    return tool_update_state(store_path, req.delta)
