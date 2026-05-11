"""Nauro MCP server — local FastAPI HTTP server exposing context tools.

Runs on localhost:7432 (configurable). No auth in v1 — local only.

MCP tools:
  - nauro.get_context(level)          → Return project context at L0/L1/L2 detail
  - nauro.propose_decision()          → Propose a decision with validation
  - nauro.confirm_decision()          → Confirm a pending decision
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
    tool_confirm_decision,
    tool_flag_question,
    tool_get_context,
    tool_propose_decision,
    tool_update_state,
)
from nauro.store.registry import (
    find_projects_by_name_v2,
    get_store_path,
    get_store_path_v2,
    resolve_project,
    resolve_v2_from_path,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
    find_repo_config,
    load_repo_config,
)

logger = logging.getLogger("nauro.mcp")

app = FastAPI(title="Nauro MCP Server", version="0.2.0")


@app.middleware("http")
async def _telemetry_transport_middleware(request, call_next):
    """Set transport='http' for the duration of this request.

    Per D117 / Phase 1c T1.5, the mcp.tool_called event reads transport from
    a ContextVar. Each FastAPI request runs in its own asyncio Task, which
    gets a copy of the parent's context — so set_transport here is scoped to
    this request and cannot leak to other transports.
    """
    from nauro.telemetry.transport import set_transport

    set_transport("http")
    return await call_next(request)


# --- Request models ---


class ContextRequest(BaseModel):
    project: str | None = None
    cwd: str | None = None
    level: int = 0  # 0, 1, or 2


class ProposeDecisionRequest(BaseModel):
    project: str
    title: str
    rationale: str
    operation: str = "add"
    affected_decision_id: str | None = None
    rejected: list[dict] | None = None
    confidence: str = "medium"
    decision_type: str | None = None
    reversibility: str | None = None
    files_affected: list[str] | None = None
    skip_validation: bool = False


class ConfirmDecisionRequest(BaseModel):
    project: str | None = None
    cwd: str | None = None
    confirm_id: str


class CheckDecisionRequest(BaseModel):
    project: str | None = None
    cwd: str | None = None
    proposed_approach: str
    context: str | None = None


class FlagQuestionRequest(BaseModel):
    project: str
    question: str
    context: str | None = None


class UpdateStateRequest(BaseModel):
    project: str
    delta: str


# --- Helpers ---


def _resolve_via_repo_config(start: Path | None) -> tuple[str, Path] | None:
    """Walk up from ``start`` (or cwd) looking for ``.nauro/config.json``."""
    config_path = find_repo_config(start=start)
    if config_path is None:
        return None
    repo_root = config_path.parent.parent
    try:
        cfg = load_repo_config(repo_root)
    except RepoConfigSchemaError:
        return None
    return cfg["id"], get_store_path_v2(cfg["id"])


def _resolve_store(project: str | None, cwd: str | None) -> Path:
    """Resolve project name to store path, raising 404 on failure.

    Same priority order as the stdio server's ``_resolve_store``: repo
    config first, then explicit project name (v2 → v1), then cwd-based
    legacy fallback.
    """
    cwd_path = Path(cwd) if cwd else None
    via_config = _resolve_via_repo_config(cwd_path)

    if project and via_config is not None:
        config_id, store_path = via_config
        if project != config_id:
            matches = find_projects_by_name_v2(project)
            if not any(pid == config_id for pid, _ in matches):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Supplied project_id {project!r} does not match the "
                        f"repo config id {config_id!r}."
                    ),
                )
        if not store_path.exists():
            raise HTTPException(status_code=404, detail=f"Project store not found: {config_id}")
        return store_path

    if via_config is not None:
        _pid, store_path = via_config
        if not store_path.exists():
            raise HTTPException(status_code=404, detail=f"Project store not found at {store_path}")
        return store_path

    if project:
        matches = find_projects_by_name_v2(project)
        if len(matches) == 1:
            pid, _entry = matches[0]
            store_path = get_store_path_v2(pid)
            if not store_path.exists():
                raise HTTPException(status_code=404, detail=f"Project store not found: {project}")
            return store_path
        if len(matches) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"Multiple v2 projects named {project!r}; pass project_id instead.",
            )
        # v1 legacy
        store_path = get_store_path(project)
        if not store_path.exists():
            raise HTTPException(status_code=404, detail=f"Project store not found: {project}")
        return store_path

    if cwd:
        legacy_name = resolve_project(Path(cwd))
        if legacy_name:
            store_path = get_store_path(legacy_name)
            if store_path.exists():
                return store_path
        v2_match = resolve_v2_from_path(Path(cwd))
        if v2_match is not None:
            pid, _entry = v2_match
            store_path = get_store_path_v2(pid)
            if store_path.exists():
                return store_path

    raise HTTPException(status_code=404, detail="Could not resolve project.")


# --- MCP tool endpoints ---


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/context")
async def get_context(req: ContextRequest) -> dict:
    """Return project context at the requested detail level (L0/L1/L2)."""
    store_path = _resolve_store(req.project, req.cwd)
    try:
        payload = tool_get_context(store_path, req.level)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"level": req.level, "content": payload}


@app.post("/propose_decision")
async def propose_decision(req: ProposeDecisionRequest) -> dict:
    """Propose a new decision with validation. Returns validation results."""
    store_path = _resolve_store(req.project, None)
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
        skip_validation=req.skip_validation,
    )


@app.post("/confirm_decision")
async def confirm_decision_endpoint(req: ConfirmDecisionRequest) -> dict:
    """Confirm a previously proposed decision."""
    store_path = _resolve_store(req.project, req.cwd)
    result = tool_confirm_decision(store_path, req.confirm_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/check_decision")
async def check_decision_endpoint(req: CheckDecisionRequest) -> dict:
    """Check for conflicts with existing decisions without writing."""
    store_path = _resolve_store(req.project, req.cwd)
    return tool_check_decision(store_path, req.proposed_approach, req.context)


# --- Legacy endpoints (kept for backwards compat, redirect to propose) ---


@app.post("/log_decision")
async def log_decision_legacy(req: ProposeDecisionRequest) -> dict:
    """Legacy endpoint — redirects to propose_decision."""
    return await propose_decision(req)


@app.post("/flag_question")
async def flag_question(req: FlagQuestionRequest) -> dict:
    """Flag an open question for human review. Checks against existing decisions."""
    store_path = _resolve_store(req.project, None)
    return tool_flag_question(store_path, req.question, req.context)


@app.post("/update_state")
async def update_state_endpoint(req: UpdateStateRequest) -> dict:
    """Update current project state. Checks for contradictions."""
    store_path = _resolve_store(req.project, None)
    return tool_update_state(store_path, req.delta)
