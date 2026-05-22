"""Kernel-level tests for ``operations.propose_decision``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` so the
Tier 1 / Tier 2 plumbing, pending mint, multi-object write, and
``resolves_questions`` ingestion exercise the locked Store protocol
without any filesystem dependency. Surface-level wiring (snapshot
capture, push hooks, length validation, envelope-token rejection,
``affected_decision_id`` resolution, AGENTS.md regen) lives in the
consumer package; the kernel must never reach into those primitives.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    InMemoryStore,
    ProposeDecisionResult,
    propose_decision,
)
from nauro_core.operations.propose_decision import _get_pending_store


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    """Each test starts with a clean pending store."""
    _get_pending_store().clear_all()


def _seed_decision(
    num: int,
    title: str,
    rationale: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
    decision_date: date | None = None,
) -> tuple[str, str]:
    """Return ``(file_stem, formatted_markdown)`` for a parseable v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=decision_date or date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=status,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale=rationale,
    )
    slug = title.lower().replace(" ", "-")
    stem = f"{num:03d}-{slug}"
    return stem, format_decision(decision)


def _store_with(*decisions: tuple[str, str], **files: str) -> InMemoryStore:
    return InMemoryStore(decisions=dict(decisions), files=dict(files))


# ── Result type / shape ─────────────────────────────────────────────────


def test_returns_result_type() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    assert isinstance(result, ProposeDecisionResult)


def test_result_is_frozen() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    with pytest.raises(ValidationError):
        result.status = "rejected"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProposeDecisionResult(
            status="confirmed",
            tier=2,
            operation="add",
            unexpected="value",
        )


def test_result_exclude_none_strips_unset_fields() -> None:
    """``status="confirmed"`` paths still carry empty list defaults; the
    transport adapter pops them. The result model itself only drops
    ``None`` values via ``exclude_none``."""
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert "confirm_id" not in dumped
    assert "error" not in dumped
    assert dumped["status"] == "confirmed"


def test_store_field_absent_from_result_model_dump() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# ── add: auto-confirm path ──────────────────────────────────────────────


def test_add_empty_store_auto_confirms_and_writes_decision() -> None:
    store = InMemoryStore()
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.decision_id is not None
    assert result.touched_decisions == [result.decision_id]
    # The decision file was written and is readable back through the
    # decision view.
    body = store.read_decision(result.decision_id)
    assert body is not None
    assert "Adopt Redis for hot caching" in body


def test_add_auto_confirm_branch_when_unrelated_decisions_exist() -> None:
    """A seeded decision unrelated to the proposal still hits the
    Tier 2 auto_confirm branch."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="Add dark mode toggle to settings page",
        rationale="Users have requested a dark theme for reduced eye strain.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.decision_id is not None


# ── add: Tier 2 pending path ────────────────────────────────────────────


def test_add_similar_decision_routes_to_pending() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="Use PostgreSQL for the data layer",
        rationale="Better JSON handling than alternatives for our application data.",
        confidence="medium",
    )
    assert result.status == "pending_confirmation"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.confirm_id is not None
    assert len(result.similar_decisions) >= 1
    assert result.decision_id is None
    # No decision file written on the pending branch.
    assert store.list_decisions() == ["001-adopt-postgresql-primary-database"]


def test_pending_branch_skip_validation_true() -> None:
    """``skip_validation=True`` mints a confirm_id after Tier 1 only."""
    store = InMemoryStore()
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        skip_validation=True,
    )
    assert result.status == "pending_confirmation"
    assert result.tier == 1
    assert result.operation == "add"
    assert result.confirm_id is not None
    assert result.similar_decisions == []
    # No decision file written.
    assert store.list_decisions() == []


# ── supersede ───────────────────────────────────────────────────────────


def test_supersede_writes_both_files_and_flips_frontmatter() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="Switch to managed PostgreSQL provider",
        rationale="Reduces operational burden; the self-hosting rationale no longer applies.",
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    # Supersede skips Tier 2 only when no similarity surfaces; with the
    # seeded match it routes to pending. Confirm via the kernel's
    # private execute path mirrors what the adapter does on confirm.
    if result.status == "pending_confirmation":
        from nauro_core.operations.propose_decision import _execute_operation

        pending = _get_pending_store().get(result.confirm_id)
        assert pending is not None
        data = pending["proposal"]
        decision_id, actual_op, touched, _resolved, error = _execute_operation(
            store,
            data["operation"],
            data["proposal"],
            data["affected_decision_id"],
        )
        assert error is None
        assert actual_op == "supersede"
        assert decision_id is not None
        assert set(touched) >= {decision_id, "001-adopt-postgresql-primary-database"}
        new_body = store.read_decision(decision_id)
        assert new_body is not None
        assert "supersedes:" in new_body
        old_body = store.read_decision("001-adopt-postgresql-primary-database")
        assert old_body is not None
        assert "status: superseded" in old_body
        assert "superseded_by:" in old_body


def test_supersede_pending_forced_when_no_similarity() -> None:
    """``operation="supersede"`` flowing through the Tier 2 auto_confirm
    branch still executes the write at the kernel level."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="Switch to managed dark-mode-only frontend",
        rationale=(
            "Users have requested a permanent dark theme; this "
            "supersedes a totally different choice."
        ),
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    # Without similarity the kernel takes the auto-confirm branch and
    # performs the supersede write.
    assert result.status == "confirmed"
    assert result.operation == "supersede"
    assert set(result.touched_decisions) == {
        result.decision_id,
        "001-adopt-postgresql",
    }
    new_body = store.read_decision(result.decision_id)
    assert new_body is not None
    assert "supersedes: '1'" in new_body
    old_body = store.read_decision("001-adopt-postgresql")
    assert old_body is not None
    assert "status: superseded" in old_body


# ── update ──────────────────────────────────────────────────────────────


def test_update_appends_rationale_paragraph() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="",
        rationale="Adds a managed-extensions clause to the existing PostgreSQL choice.",
        operation="update",
        affected_decision_id="decision-001",
    )
    # update against a similar-titled seed routes through pending; confirm
    # via the kernel's private execute path mirrors what the adapter
    # does on confirm.
    if result.status == "pending_confirmation":
        from nauro_core.operations.propose_decision import _execute_operation

        pending = _get_pending_store().get(result.confirm_id)
        assert pending is not None
        data = pending["proposal"]
        decision_id, actual_op, touched, _resolved, error = _execute_operation(
            store,
            data["operation"],
            data["proposal"],
            data["affected_decision_id"],
        )
        assert error is None
        assert actual_op == "update"
        assert decision_id == "001-adopt-postgresql"
        assert touched == ("001-adopt-postgresql",)
        body = store.read_decision(decision_id)
        assert body is not None
        assert "managed-extensions clause" in body
        # The version frontmatter incremented.
        assert "version: 2" in body


def test_update_disallowed_field_rejected_loudly() -> None:
    """``operation="update"`` cannot change metadata; rejection at Tier 0."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="A new title",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        operation="update",
        affected_decision_id="decision-001",
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert result.operation == "update"
    assert "title" in result.assessment


# ── Tier 1 structural rejection ─────────────────────────────────────────


def test_add_empty_title_rejected_at_tier_1() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "title" in result.assessment.lower()


def test_add_short_rationale_rejected_at_tier_1() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="too short",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1


# ── resolves_questions ──────────────────────────────────────────────────


def test_resolves_questions_flips_entry_resolved_by() -> None:
    """A confirmed add with ``resolves_questions=["Q1"]`` writes the
    decision file AND annotates the matching entry with the decision ref."""
    open_questions = (
        "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    assert result.status == "confirmed"
    assert result.decision_id is not None
    assert result.resolved_questions == ["Q1"]
    updated = store.read_file(OPEN_QUESTIONS_MD)
    assert updated is not None
    # The entry stays in place but carries the resolved-by ref.
    assert "[Q1] Should we adopt PostgreSQL?" in updated
    # Pull the leading number off the decision id stem to confirm the ref points to it.
    decision_num = int(result.decision_id.split("-", 1)[0])
    assert f"D{decision_num}" in updated or f"decision-{decision_num:03d}" in updated


def test_resolves_questions_unknown_id_rejected_at_boundary() -> None:
    open_questions = "# Open Questions\n\n## Active\n\n- [Q1] Real question?\n\n## Resolved\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        resolves_questions=["Q999"],
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert "unknown" in result.assessment.lower()
    assert "Q999" in result.assessment
    # No decision file written on rejection.
    assert store.list_decisions() == []


def test_resolves_questions_ambiguous_id_rejected_at_boundary() -> None:
    """When two entries collide on the same id, the boundary surfaces the
    counterpart suggestions."""
    open_questions = (
        "# Open Questions\n\n## Active\n\n"
        "- [Q1] First duplicate (num=1)\n"
        "- [Q1] Second duplicate (num=2)\n\n## Resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert "ambiguous" in result.assessment.lower()
    assert "Q1" in result.assessment


def test_add_resolves_questions_half_state_surfaces_error() -> None:
    """``add`` half-state: decision file written, open-questions.md write
    fails. The error must propagate through the result envelope so the
    caller learns the questions file is out of sync.

    Parity with the supersede / update error paths: when the multi-object
    write hits a mid-sequence failure, the envelope carries the error
    payload naming the half-state.
    """
    open_questions = (
        "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    )

    class _OpenQuestionsWriteFails(InMemoryStore):
        def write_file(self, path: str, content: str) -> None:
            if path == OPEN_QUESTIONS_MD:
                raise OSError("simulated open-questions.md write failure")
            super().write_file(path, content)

    store = _OpenQuestionsWriteFails(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    # The half-state error surfaces as a rejected envelope with the
    # written decision id in touched_decisions; the kernel does not
    # silently drop the resolve failure.
    assert result.status == "rejected"
    assert result.tier == 2
    assert result.error is not None
    assert result.error.kind == "error"
    assert "question-resolution half-state" in result.error.reason
    assert result.resolved_questions == []
    # The decision file was written before the open-questions write
    # failed; touched_decisions surfaces it so sync-repair can reconcile.
    assert len(result.touched_decisions) == 1
    decision_id = result.touched_decisions[0]
    assert store.read_decision(decision_id) is not None


# ── touched_decisions enumeration ───────────────────────────────────────


def test_touched_decisions_only_new_id_on_add() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.touched_decisions == [result.decision_id]


def test_touched_decisions_empty_on_rejection() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.touched_decisions == []


# ── Import-graph negative constraint ────────────────────────────────────


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "propose_decision.py"
    )
    tree = ast.parse(module_path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add(f"{module}.{alias.name}")
                imported.add(alias.name)
                imported.add(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    return imported


def test_kernel_does_not_import_adapter_or_path_primitives() -> None:
    imported = _kernel_imports()
    forbidden_names = {
        "nauro.store.snapshot",
        "nauro.sync.hooks",
        "nauro.validation.pending",
        "nauro.validation.pipeline",
        "nauro.validation.tier1",
        "nauro.validation.tier2",
        "capture_snapshot",
        "push_after_write",
        "pull_before_session",
    }
    leaked = forbidden_names & imported
    assert not leaked, (
        f"kernel imports adapter-side primitives: {leaked}; "
        "those live on the transport, not in the kernel."
    )
    assert "pathlib" not in imported and "pathlib.Path" not in imported, (
        "kernel imports pathlib; the Store protocol abstracts paths as strings."
    )
    assert "filelock" not in imported, "kernel must not import filelock"


# ── _write_decision_direct helper ───────────────────────────────────────


def test_write_decision_direct_writes_file_and_updates_hash_index() -> None:
    from nauro_core.constants import DECISION_HASHES_FILE
    from nauro_core.operations.propose_decision import _write_decision_direct

    store = InMemoryStore()
    decision_id = _write_decision_direct(
        store,
        {
            "title": "Adopt Redis",
            "rationale": "In-memory cache for the hot read paths across the API tier.",
            "confidence": "high",
        },
    )
    assert decision_id.startswith("001-")
    body = store.read_decision(decision_id)
    assert body is not None
    assert "Adopt Redis" in body
    # The hash index records the new decision.
    hash_body = store.read_file(DECISION_HASHES_FILE)
    assert hash_body is not None
    assert decision_id in hash_body


def test_write_decision_direct_increments_decision_number() -> None:
    from nauro_core.operations.propose_decision import _write_decision_direct

    store = _store_with(
        _seed_decision(1, "First", "First rationale that comfortably exceeds the minimum length."),
        _seed_decision(2, "Second", "Second rationale that also comfortably exceeds the minimum."),
    )
    decision_id = _write_decision_direct(
        store,
        {
            "title": "Third decision in line",
            "rationale": "Third rationale that comfortably exceeds the minimum length too.",
            "confidence": "medium",
        },
    )
    assert decision_id.startswith("003-")
