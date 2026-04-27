"""Nauro MCP server — local FastAPI HTTP server exposing context tools.

Runs on localhost:7432 (configurable). No auth in v1 — local only.

MCP tools:
  - nauro.get_context(level)          → Return project context at L0/L1/L2 detail
  - nauro.propose_decision()          → Propose a decision with validation
  - nauro.confirm_decision()          → Confirm a pending decision
  - nauro.check_decision()            → Check for conflicts without writing
  - nauro.flag_question()             → Flag an open question for human review
  - nauro.update_state()              → Update current project state

Hook endpoints (called by Claude Code's hook system):
  - POST /hooks/pre-compact    → Log compaction start
  - POST /hooks/post-compact   → Extract decisions from compaction summary
  - POST /hooks/session-start  → Return context for injection
"""

from __future__ import annotations

import asyncio
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
from nauro.store.reader import read_project_context
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


# --- Request models ---


class ContextRequest(BaseModel):
    project: str | None = None
    cwd: str | None = None
    level: int = 0  # 0, 1, or 2


class ProposeDecisionRequest(BaseModel):
    project: str
    title: str
    rationale: str
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


class HookRequest(BaseModel):
    session_id: str | None = None
    cwd: str | None = None
    hook_event_name: str | None = None
    source: str | None = None


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


def _resolve_store_safe(cwd: str | None) -> Path | None:
    """Resolve project store from cwd, returning None on failure (never raises)."""
    if not cwd:
        return None
    try:
        via_config = _resolve_via_repo_config(Path(cwd))
        if via_config is not None:
            _pid, store_path = via_config
            return store_path if store_path.exists() else None

        v2_match = resolve_v2_from_path(Path(cwd))
        if v2_match is not None:
            pid, _entry = v2_match
            store_path = get_store_path_v2(pid)
            return store_path if store_path.exists() else None

        name = resolve_project(Path(cwd))
        if not name:
            return None
        store_path = get_store_path(name)
        return store_path if store_path.exists() else None
    except Exception:
        return None


def _resolve_project_key_safe(cwd: str | None) -> str | None:
    """Return the project_id (or legacy name) for the cwd, or None."""
    if not cwd:
        return None
    try:
        via_config = _resolve_via_repo_config(Path(cwd))
        if via_config is not None:
            pid, _store = via_config
            return pid
        v2_match = resolve_v2_from_path(Path(cwd))
        if v2_match is not None:
            pid, _entry = v2_match
            return pid
        return resolve_project(Path(cwd))
    except Exception:
        return None


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


# --- Hook endpoints ---
# These are called by Claude Code's hook system via HTTP POST.
# MUST always return 200, even on error, to avoid Claude Code disabling hooks.


@app.post("/hooks/pre-compact")
async def hook_pre_compact(req: HookRequest) -> dict:
    """Called before Claude Code compacts the conversation."""
    logger.info(
        "pre-compact hook: session=%s cwd=%s",
        req.session_id,
        req.cwd,
    )
    return {}


@app.post("/hooks/post-compact")
async def hook_post_compact(req: HookRequest) -> dict:
    """Called after Claude Code compacts the conversation.

    Primary extraction trigger. Reads the compaction summary and runs extraction
    through the validation pipeline.
    """
    try:
        store_path = _resolve_store_safe(req.cwd)
        if not store_path:
            logger.warning("post-compact: could not resolve project for cwd=%s", req.cwd)
            return {"status": "no_project"}

        result = await asyncio.to_thread(
            _run_post_compact_extraction,
            store_path=store_path,
            session_id=req.session_id,
            cwd=req.cwd,
        )
        return result

    except Exception as e:
        logger.exception("post-compact hook error: %s", e)
        return {"status": "error", "message": str(e)}


@app.post("/hooks/session-start")
async def hook_session_start(req: HookRequest) -> dict:
    """Called when a Claude Code session starts or after compaction.

    Pulls latest from S3 before returning context so every session
    starts with the most recent remote state.
    """
    try:
        store_path = _resolve_store_safe(req.cwd)
        if not store_path:
            return {"context": ""}

        # Pull latest from S3 before reading context (non-blocking on failure)
        try:
            project_key = _resolve_project_key_safe(req.cwd)
            if project_key:
                from nauro.sync.hooks import pull_before_session

                await asyncio.to_thread(pull_before_session, project_key, store_path)
        except Exception:
            logger.warning("session-start: S3 pull failed, continuing with local state")

        context = read_project_context(store_path, level=0)
        return {"context": context}

    except Exception as e:
        logger.exception("session-start hook error: %s", e)
        return {"context": ""}


def _run_post_compact_extraction(
    store_path: Path,
    session_id: str | None,
    cwd: str | None,
) -> dict:
    """Run extraction from compaction summary through validation pipeline."""
    # TODO: convert session_extractor to ExtractionOutcome (deferred from D63)
    from nauro.extraction.pipeline import _append_extraction_log, route_extraction_to_store
    from nauro.extraction.session_extractor import (
        extract_from_compaction,
        find_session_jsonl,
        read_compaction_from_session,
    )
    from nauro.extraction.signal import from_dict

    if not session_id:
        return {"status": "no_session_id"}

    session_path = find_session_jsonl(session_id, cwd=cwd)
    if not session_path:
        logger.warning("post-compact: session file not found for %s", session_id)
        return {"status": "session_not_found"}

    summary = read_compaction_from_session(session_path)
    if not summary:
        logger.info("post-compact: no compaction summary found in session %s", session_id)
        return {"status": "no_compaction_summary"}

    result = extract_from_compaction(summary, store_path, session_id=session_id)

    # Handle no-API-key skip
    if result.get("reasoning") == "no_api_key":
        _append_extraction_log(
            store_path,
            {
                "source": "compaction",
                "session_id": session_id,
                "signal": {},
                "composite_score": None,
                "skip": True,
                "reasoning": "no_api_key",
                "captured": False,
            },
        )
        return {"status": "skipped", "reasoning": "no_api_key"}

    signal = from_dict(result)
    _append_extraction_log(
        store_path,
        {
            "source": "compaction",
            "session_id": session_id,
            "signal": signal.to_dict(),
            "composite_score": signal.composite_score,
            "skip": result.get("skip", True),
            "reasoning": signal.reasoning,
            "captured": not result.get("skip") and signal.composite_score >= 0.4,
        },
    )

    if result.get("skip") or signal.composite_score < 0.4:
        return {"status": "skipped", "composite_score": signal.composite_score}

    # Route through validation pipeline
    route_extraction_to_store(
        result,
        store_path,
        source="compaction",
        session_id=session_id,
        trigger=f"compaction (session {session_id})",
    )

    # Push to S3 after extraction (event-driven sync)
    try:
        project_key = _resolve_project_key_safe(cwd)
        if project_key:
            from nauro.sync.hooks import push_after_extraction

            push_after_extraction(project_key, store_path)
    except Exception:
        logger.warning("post-compact: S3 push failed, decisions saved locally")

    n_decisions = len(result.get("decisions", []))
    n_questions = len(result.get("questions", []))
    return {
        "status": "extracted",
        "decisions": n_decisions,
        "questions": n_questions,
        "composite_score": signal.composite_score,
    }
