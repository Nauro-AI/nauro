"""Canonical MCP tool implementations — transport-agnostic.

Both the HTTP server (server.py) and stdio server (stdio_server.py) delegate
to these functions. All results are returned as dicts; each transport layer may
reformat the output (e.g. stdio converts flag_question and update_state to
strings for FastMCP compatibility).
"""

from __future__ import annotations

import logging
from pathlib import Path

from nauro_core.constants import (
    MAX_CONTEXT_LENGTH,
    MAX_DELTA_LENGTH,
    MAX_QUESTION_LENGTH,
    MAX_RATIONALE_LENGTH,
    MAX_TITLE_LENGTH,
    STATE_CURRENT_FILENAME,
)
from nauro_core.operations import check_decision as _check_decision_op
from nauro_core.operations import get_decision as _get_decision_op
from nauro_core.operations import get_raw_file as _get_raw_file_op
from nauro_core.operations import list_decisions as _list_decisions_op
from nauro_core.operations import search_decisions as _search_decisions_op
from nauro_core.protocol import (
    CHECK_DECISION_RETURNS,
    GET_DECISION_BEFORE_PROPOSING,
    PROPOSE_DECISION_OPERATIONS,
    UPDATE_SUPERSEDE_CARE,
)
from nauro_core.validation import check_content_length, find_envelope_token

from nauro.mcp.payloads import build_l0_payload, build_l1_payload, build_l2_payload
from nauro.onboarding import (
    NO_CONTEXT_YET,
    WELCOME_NO_PROJECT,
)
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.reader import (
    diff_since_last_session as _diff_since_last_session,
)
from nauro.store.reader import (
    resolve_decision_id,
)
from nauro.store.snapshot import capture_snapshot
from nauro.store.writer import append_question
from nauro.store.writer import update_state as _write_state
from nauro.telemetry.decorators import mcp_tool
from nauro.validation.pipeline import confirm_write, validate_proposed_write
from nauro.validation.tier2 import check_similarity

logger = logging.getLogger("nauro.mcp.tools")

# Single source of truth for stop-words used in contradiction checks
_STOP_WORDS = {"the", "a", "an", "to", "and", "or", "is", "was", "-"}


def _reject_if_too_long(value: str, label: str, max_length: int) -> dict | None:
    """Return a rejection dict if *value* exceeds *max_length*, else None."""
    msg = check_content_length(value, label, max_length)
    if msg:
        return {"store": "local", "status": "rejected", "reason": msg}
    return None


def _reject_if_envelope_token(value: str, field_name: str) -> dict | None:
    """Return a rejection dict if *value* contains an MCP envelope fragment.

    Some non-Anthropic agent surfaces emit tool calls as XML and their MCP
    bridges occasionally fail to extract <parameter> values cleanly, so the
    envelope tail leaks into the string field. Reject before any I/O — see
    nauro_core.validation.find_envelope_token.
    """
    token = find_envelope_token(value)
    if not token:
        return None
    return {
        "store": "local",
        "status": "rejected",
        "reason": (
            f"{field_name} contains tool-use envelope fragment {token!r}. "
            "This usually means the client failed to extract the parameter "
            "value cleanly from an XML tool call. Resend the call with just "
            "the prose content."
        ),
    }


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


@mcp_tool("get_context")
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


@mcp_tool("propose_decision")
def tool_propose_decision(
    store_path: Path,
    title: str = "",
    rationale: str = "",
    operation: str = "add",
    affected_decision_id: str | None = None,
    rejected: list[dict] | None = None,
    confidence: str | None = None,
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    resolves_questions: list[str] | None = None,
    skip_validation: bool = False,
) -> dict:
    """Propose a new decision through the validation pipeline."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    for err in (
        _reject_if_too_long(title, "Title", MAX_TITLE_LENGTH),
        _reject_if_too_long(rationale, "Rationale", MAX_RATIONALE_LENGTH),
    ):
        if err:
            return err

    # Reject tool-use envelope fragments that leaked from XML-emitting clients.
    envelope_targets: list[tuple[str, str]] = [
        ("title", title),
        ("rationale", rationale),
    ]
    for idx, item in enumerate(rejected or []):
        if isinstance(item, dict):
            envelope_targets.append((f"rejected[{idx}].reason", item.get("reason", "") or ""))
    for field_name, value in envelope_targets:
        err = _reject_if_envelope_token(value, field_name)
        if err:
            return err

    if operation in ("update", "supersede"):
        if not affected_decision_id:
            return {
                "store": "local",
                "status": "rejected",
                "reason": f"operation={operation!r} requires affected_decision_id",
            }
        resolved = resolve_decision_id(store_path, affected_decision_id)
        if resolved is None:
            return {
                "store": "local",
                "status": "rejected",
                "reason": (f"affected_decision_id={affected_decision_id!r} not found in store"),
            }
        affected_decision_id = resolved

    # confidence flows as None when the caller did not send it so the
    # validation pipeline can distinguish caller intent from default.
    # Reapply "medium" only for add/supersede where the value lands in the
    # decision file; on update the disallowed-fields branch uses the unset
    # value to recognise "caller did not send confidence".
    proposal_confidence: str | None
    if operation in ("add", "supersede") and confidence is None:
        proposal_confidence = "medium"
    else:
        proposal_confidence = confidence

    proposal = {
        "title": title,
        "rationale": rationale,
        "rejected": rejected,
        "confidence": proposal_confidence,
        "decision_type": decision_type,
        "reversibility": reversibility,
        "files_affected": files_affected,
        "resolves_questions": list(resolves_questions) if resolves_questions else [],
        "source": "mcp",
    }

    result = validate_proposed_write(
        proposal,
        store_path,
        skip_validation=skip_validation,
        operation=operation,
        affected_decision_id=affected_decision_id,
    )

    response: dict = {
        "store": "local",
        "status": result.status,
        "validation": {
            "tier": result.tier,
            "operation": result.operation,
            "similar_decisions": result.similar_decisions,
            "assessment": result.assessment,
        },
    }

    if result.status == "confirmed" and hasattr(result, "_decision_id"):
        response["decision_id"] = result._decision_id
    if result.confirm_id:
        response["confirm_id"] = result.confirm_id
    if result.resolved_questions:
        response["resolved_questions"] = list(result.resolved_questions)

    return response


# Compose the agent-facing docstring from canonical fragments so the
# operation classification advice cannot drift from MCP_INSTRUCTIONS_STATIC
# or the propose_decision ToolSpec. Guarded by
# test_retired_paraphrase_absent in packages/nauro/tests/test_protocol_drift.py.
tool_propose_decision.__doc__ = f"""\
Propose a new decision through the validation pipeline.

Args:
    title: Short title for the decision.
    rationale: Why this decision is being made.
    operation: How this proposal relates to existing decisions.

        {PROPOSE_DECISION_OPERATIONS}

        {UPDATE_SUPERSEDE_CARE}

    affected_decision_id: Required when ``operation`` is ``update`` or
        ``supersede``. The id (e.g. "decision-042") being modified.
    rejected: List of {{alternative, reason}} dicts.
    confidence: "high" | "medium" | "low".
    decision_type: Optional category string.
    reversibility: Optional "easy" | "moderate" | "hard".
    files_affected: Optional list of file paths.
    skip_validation: When True, skip Tier 2 and queue a confirm_id after
        Tier 1 passes. Use when the caller already ran ``check_decision``.
"""


@mcp_tool("confirm_decision")
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


@mcp_tool("check_decision")
def tool_check_decision(
    store_path: Path,
    proposed_approach: str,
    context: str | None = None,
) -> dict:
    """Check for conflicts with existing decisions without writing anything."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    result = _check_decision_op(FilesystemStore(store_path), proposed_approach, context)
    return {"store": "local", **result.model_dump(mode="json", exclude_none=True)}


# Compose the agent-facing docstring from canonical fragments so the
# read-then-judge protocol cannot drift from MCP_INSTRUCTIONS_STATIC or
# the check_decision ToolSpec. Guarded by test_retired_paraphrase_absent
# in packages/nauro/tests/test_protocol_drift.py.
tool_check_decision.__doc__ = f"""\
Check for conflicts with existing decisions without writing anything.

{CHECK_DECISION_RETURNS}

{GET_DECISION_BEFORE_PROPOSING}
"""


@mcp_tool("flag_question")
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

    # Reject tool-use envelope fragments that leaked from XML-emitting clients.
    for field_name, value in (("question", question), ("context", context or "")):
        err = _reject_if_envelope_token(value, field_name)
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


@mcp_tool("get_raw_file")
def tool_get_raw_file(store_path: Path, path: str) -> dict:
    """Return raw content of any file in the project store."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}

    # Adapter-side traversal check. Distinct from the kernel-side
    # file-not-found case so callers get a clear "Invalid path" signal
    # before any Store I/O.
    resolved = (store_path / path).resolve()
    try:
        resolved.relative_to(store_path.resolve())
    except ValueError:
        return {
            "store": "local",
            "error": {"kind": "error", "reason": f"Invalid path: {path}"},
        }

    result = _get_raw_file_op(FilesystemStore(store_path), path)
    envelope: dict = {"store": "local", **result.model_dump(mode="json", exclude_none=True)}

    # On miss, build the "available files" hint locally — file
    # enumeration outside the decisions/ directory is outside the
    # Store protocol's locked surface. Cap at 20 entries so the miss
    # envelope stays bounded for stores with many markdown files.
    if result.error is not None:
        available = []
        for f in sorted(store_path.rglob("*.md")):
            rel = f.relative_to(store_path)
            if not str(rel).startswith("snapshots/"):
                available.append(str(rel))
        envelope["available_files"] = available[:20]

    return envelope


@mcp_tool("list_decisions")
def tool_list_decisions(
    store_path: Path,
    limit: int = 20,
    include_superseded: bool = False,
) -> dict:
    """List decision summaries, sorted by number descending."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    result = _list_decisions_op(FilesystemStore(store_path), limit, include_superseded)
    return {"store": "local", **result.model_dump(mode="json", exclude_none=True)}


@mcp_tool("get_decision")
def tool_get_decision(store_path: Path, number: int) -> dict:
    """Return full content of a specific decision by number."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    result = _get_decision_op(FilesystemStore(store_path), number)
    return {"store": "local", **result.model_dump(mode="json", exclude_none=True)}


@mcp_tool("diff_since_last_session")
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


@mcp_tool("search_decisions")
def tool_search_decisions(
    store_path: Path,
    query: str,
    limit: int = 10,
) -> dict:
    """Search decisions by keyword. Returns matching decisions with snippets."""
    guidance = _check_store_exists(store_path)
    if guidance:
        return {"store": "local", "status": "error", "guidance": guidance}
    result = _search_decisions_op(FilesystemStore(store_path), query, limit)
    return {"store": "local", **result.model_dump(mode="json", exclude_none=True)}


@mcp_tool("update_state")
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
    state_path = store_path / STATE_CURRENT_FILENAME
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
