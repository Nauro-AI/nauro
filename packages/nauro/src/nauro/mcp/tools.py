"""Canonical MCP tool implementations — transport-agnostic.

Both the HTTP server (server.py) and stdio server (stdio_server.py) delegate
to these functions. All results are returned as dicts; each transport layer may
reformat the output (e.g. stdio converts flag_question and update_state to
strings for FastMCP compatibility).
"""

from __future__ import annotations

import logging
from pathlib import Path

from nauro_core import extract_decision_number
from nauro_core.constants import (
    MAX_APPROACH_LENGTH,
    MAX_CONTEXT_LENGTH,
    MAX_DELTA_LENGTH,
    MAX_QUESTION_LENGTH,
    MAX_RATIONALE_LENGTH,
    MAX_TITLE_LENGTH,
)
from nauro_core.validation import check_content_length

from nauro.mcp.payloads import build_l0_payload, build_l1_payload, build_l2_payload
from nauro.onboarding import (
    NO_CONTEXT_YET,
    NO_DECISIONS_TO_CHECK,
    WELCOME_NO_PROJECT,
)
from nauro.store.config import get_config
from nauro.store.reader import (
    _list_decisions,
    search_decisions,
)
from nauro.store.reader import (
    diff_since_last_session as _diff_since_last_session,
)
from nauro.store.snapshot import capture_snapshot
from nauro.store.writer import append_question
from nauro.store.writer import update_state as _write_state
from nauro.validation.pipeline import confirm_write, validate_proposed_write
from nauro.validation.tier2 import check_similarity
from nauro.validation.tier3 import check_conflicts_with_llm

logger = logging.getLogger("nauro.mcp.tools")

# Single source of truth for stop-words used in contradiction checks
_STOP_WORDS = {"the", "a", "an", "to", "and", "or", "is", "was", "-"}


def _reject_if_too_long(value: str, label: str, max_length: int) -> dict | None:
    """Return a rejection dict if *value* exceeds *max_length*, else None."""
    msg = check_content_length(value, label, max_length)
    if msg:
        return {"store": "local", "status": "rejected", "reason": msg}
    return None


def _check_store_exists(store_path: Path) -> str | None:
    """Return guidance string if the store is missing, None if it exists."""
    if not store_path.exists():
        return WELCOME_NO_PROJECT
    return None


def _has_decisions(store_path: Path) -> bool:
    """Check whether the store has any decision files."""
    decisions_dir = store_path / "decisions"
    if not decisions_dir.exists():
        return False
    return any(decisions_dir.glob("*.md"))


def _try_push(store_path: Path) -> None:
    """Best-effort push to S3 after a local write. Never raises."""
    try:
        from nauro.sync.hooks import push_after_write

        project_name = store_path.name
        push_after_write(project_name, store_path)
    except Exception:
        logger.debug("sync push after write failed for %s", store_path.name, exc_info=True)


def _coerce_level(level: int | str) -> int:
    """Coerce level to int. Accepts int (0/1/2) or string ("L0"/"L1"/"L2")."""
    if isinstance(level, str):
        mapping = {"L0": 0, "L1": 1, "L2": 2}
        coerced = mapping.get(level.upper())
        if coerced is None:
            raise ValueError(f"Invalid level: {level!r}. Use 0, 1, 2 or 'L0', 'L1', 'L2'.")
        return coerced
    return level


def tool_get_context(store_path: Path, level: int | str) -> str:
    """Return project context at the requested detail level."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return guidance
    level_int = _coerce_level(level)
    builders = {0: build_l0_payload, 1: build_l1_payload, 2: build_l2_payload}
    builder = builders.get(level_int)
    if builder is None:
        raise ValueError(f"Invalid level: {level}. Use 0, 1, 2 or 'L0', 'L1', 'L2'.")
    result = builder(store_path)
    if not result.strip() or not _has_decisions(store_path):
        return (result + "\n\n" + NO_CONTEXT_YET).strip() if result.strip() else NO_CONTEXT_YET
    return result


def tool_propose_decision(
    store_path: Path,
    title: str,
    rationale: str,
    rejected: list[dict] | None = None,
    confidence: str = "medium",
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    skip_validation: bool = False,
) -> dict:
    """Propose a new decision through the validation pipeline."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    # Content size limits
    for err in (
        _reject_if_too_long(title, "Title", MAX_TITLE_LENGTH),
        _reject_if_too_long(rationale, "Rationale", MAX_RATIONALE_LENGTH),
    ):
        if err:
            return err

    proposal = {
        "title": title,
        "rationale": rationale,
        "rejected": rejected,
        "confidence": confidence,
        "decision_type": decision_type,
        "reversibility": reversibility,
        "files_affected": files_affected,
        "source": "mcp",
    }

    result = validate_proposed_write(
        proposal,
        store_path,
        auto_confirm=False,
        api_key=get_config("api_key"),
        skip_validation=skip_validation,
    )

    response: dict = {
        "store": "local",
        "status": result.status,
        "validation": {
            "tier": result.tier,
            "operation": result.operation,
            "similar_decisions": result.similar_decisions,
            "conflicts": result.conflicts,
            "assessment": result.assessment,
            "suggested_refinements": result.suggested_refinements,
        },
    }

    if result.status == "confirmed" and hasattr(result, "_decision_id"):
        response["decision_id"] = result._decision_id
    if result.confirm_id:
        response["confirm_id"] = result.confirm_id

    return response


def tool_confirm_decision(store_path: Path, confirm_id: str) -> dict:
    """Confirm a previously proposed decision."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    result = confirm_write(confirm_id, store_path)
    response = {"store": "local", **result}
    if result.get("status") == "confirmed":
        _try_push(store_path)
    return response


def tool_check_decision(
    store_path: Path,
    proposed_approach: str,
    context: str | None = None,
) -> dict:
    """Check for conflicts with existing decisions without writing anything."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    # Content size limits
    for err in (
        _reject_if_too_long(proposed_approach, "Proposed approach", MAX_APPROACH_LENGTH),
        _reject_if_too_long(context or "", "Context", MAX_CONTEXT_LENGTH) if context else None,
    ):
        if err:
            return err

    if not _has_decisions(store_path):
        return {
            "store": "local",
            "related_decisions": [],
            "potential_conflicts": [],
            "assessment": NO_DECISIONS_TO_CHECK,
        }
    pseudo_proposal = {
        "title": proposed_approach[:100],
        "rationale": proposed_approach + (f" {context}" if context else ""),
    }

    t2_action, similar_decisions = check_similarity(pseudo_proposal, store_path)

    if t2_action == "auto_confirm" or not similar_decisions:
        return {
            "store": "local",
            "related_decisions": [],
            "potential_conflicts": [],
            "assessment": (
                "No existing decisions found that relate to this"
                " approach. Proceed and consider logging the decision."
            ),
        }

    llm_result = check_conflicts_with_llm(
        proposed_approach,
        context,
        similar_decisions,
        store_path,
        api_key=get_config("api_key"),
    )

    all_decisions = _list_decisions(store_path)
    decision_titles = {f"decision-{d['num']:03d}": d["title"] for d in all_decisions}

    related = []
    for rd in llm_result.get("related_decisions", []):
        did = rd.get("decision_id", "")
        related.append(
            {
                "id": did,
                "title": decision_titles.get(did, ""),
                "relevance": rd.get("relevance", "medium"),
                "rationale_preview": "",
            }
        )

    return {
        "store": "local",
        "related_decisions": related,
        "potential_conflicts": llm_result.get("potential_conflicts", []),
        "assessment": llm_result.get("assessment", ""),
    }


def tool_flag_question(
    store_path: Path,
    question: str,
    context: str | None = None,
) -> dict:
    """Flag an open question for human review. Always writes the question."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    # Content size limits
    for err in (
        _reject_if_too_long(question, "Question", MAX_QUESTION_LENGTH),
        _reject_if_too_long(context or "", "Context", MAX_CONTEXT_LENGTH) if context else None,
    ):
        if err:
            return err

    pseudo_proposal = {
        "title": question[:100],
        "rationale": question + (f" {context}" if context else ""),
    }

    hint = None
    try:
        _, similar = check_similarity(pseudo_proposal, store_path)
        if similar and similar[0].get("similarity", 0) > 0.7:
            top = similar[0]
            hint = f"This question appears to be addressed by {top['id']}: {top['title']}."
    except Exception:
        pass

    text = question
    if context:
        text = f"{question} (context: {context})"
    append_question(store_path, text)
    capture_snapshot(store_path, trigger=f"question: {question}")

    response: dict = {"store": "local", "status": "ok"}
    if hint:
        response["hint"] = hint
    _try_push(store_path)
    return response


def tool_get_raw_file(store_path: Path, path: str) -> dict:
    """Return raw content of any file in the project store."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    file_path = store_path / path
    # Prevent path traversal
    try:
        file_path.resolve().relative_to(store_path.resolve())
    except ValueError:
        return {"store": "local", "error": f"Invalid path: {path}"}
    if not file_path.exists():
        # List available files as a hint
        available = []
        for f in sorted(store_path.rglob("*.md")):
            rel = f.relative_to(store_path)
            if not str(rel).startswith("snapshots/"):
                available.append(str(rel))
        hint = ""
        if available:
            hint = "\n\nAvailable files:\n" + "\n".join(f"- {f}" for f in available[:20])
        return {"store": "local", "error": f"File not found: {path}{hint}"}
    return {"store": "local", "content": file_path.read_text()}


def tool_list_decisions(
    store_path: Path,
    limit: int = 20,
    include_superseded: bool = False,
) -> dict:
    """List decision summaries, sorted by number descending."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    decisions = _list_decisions(store_path)
    if not include_superseded:
        decisions = [d for d in decisions if d.get("status", "active") == "active"]
    decisions.sort(key=lambda d: d["num"], reverse=True)
    items = []
    for d in decisions[:limit]:
        items.append(
            {
                "number": d["num"],
                "title": d["title"],
                "date": d.get("date"),
                "status": d.get("status", "active"),
                "type": d.get("decision_type"),
                "confidence": d.get("confidence"),
            }
        )
    return {"store": "local", "decisions": items}


def tool_get_decision(store_path: Path, number: int) -> dict:
    """Return full content of a specific decision by number."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    decisions_dir = store_path / "decisions"
    if decisions_dir.exists():
        for f in sorted(decisions_dir.glob("*.md")):
            n = extract_decision_number(f.name)
            if n is not None and n == number:
                return {"store": "local", "content": f.read_text()}
    return {"store": "local", "error": f"Decision {number} not found"}


def tool_diff_since_last_session(
    store_path: Path,
    days: int | None = None,
) -> dict:
    """Show what changed since the last session or N days ago."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    diff = _diff_since_last_session(store_path, days)
    return {"store": "local", "diff": diff}


def tool_search_decisions(
    store_path: Path,
    query: str,
    limit: int = 10,
) -> dict:
    """Search decisions by keyword. Returns matching decisions with snippets."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    return search_decisions(store_path, query, limit)


def tool_update_state(store_path: Path, delta: str) -> dict:
    """Update current project state. Returns a warning on keyword overlap."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    # Content size limits
    err = _reject_if_too_long(delta, "Delta", MAX_DELTA_LENGTH)
    if err:
        return err

    warning = None
    state_path = store_path / "state.md"
    if state_path.exists():
        state_content = state_path.read_text()
        delta_words = set(delta.lower().split())
        for line in state_content.split("\n"):
            if line.startswith("- ") and "none yet" not in line:
                line_words = set(line.lower().split())
                overlap = delta_words & line_words - _STOP_WORDS
                if len(overlap) >= 3:
                    warning = f"State update shares keywords with existing entry: {line.strip()}"
                    break

    _write_state(store_path, delta)
    capture_snapshot(store_path, trigger=f"state: {delta}")

    response: dict = {"store": "local", "status": "ok"}
    if warning:
        response["warning"] = warning
    _try_push(store_path)
    return response
