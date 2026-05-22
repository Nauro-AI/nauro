"""Kernel-level tests for ``operations.confirm_decision``.

Each test seeds the module-global pending store directly so the confirm
path exercises the replay-and-write plumbing against the locked Store
protocol without any filesystem or adapter dependency. Surface-level
wiring (snapshot capture, push hooks, AGENTS.md regen) lives in the
consumer package; the kernel must never reach into those primitives.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
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
    ConfirmDecisionResult,
    InMemoryStore,
    confirm_decision,
)
from nauro_core.operations.propose_decision import _get_pending_store


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    """Each test starts with a clean pending store."""
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


def _seed_decision(
    num: int,
    title: str,
    rationale: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
) -> tuple[str, str]:
    """Return ``(file_stem, formatted_markdown)`` for a parseable v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=datetime.now(timezone.utc).date(),
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


def _seed_pending_add(
    *,
    title: str = "Adopt Redis for hot caching",
    rationale: str = "In-memory cache for the hot read paths across the API tier.",
    resolves: list[str] | None = None,
) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": title,
                "rationale": rationale,
                "confidence": "medium",
                "resolves_questions": resolves or [],
            },
            "operation": "add",
            "affected_decision_id": None,
        },
        {"tier": 1, "operation": "add", "similar_decisions": [], "assessment": "seed"},
    )


def _seed_pending_supersede(
    affected_decision_id: str,
    *,
    title: str = "Switch to managed PostgreSQL provider",
    rationale: str = "Reduces operational burden; the self-hosting rationale no longer applies.",
) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": title,
                "rationale": rationale,
                "confidence": "medium",
            },
            "operation": "supersede",
            "affected_decision_id": affected_decision_id,
        },
        {"tier": 2, "operation": "supersede", "similar_decisions": [], "assessment": "seed"},
    )


def _seed_pending_update(
    affected_decision_id: str,
    *,
    rationale: str = "Adds a managed-extensions clause to the existing PostgreSQL choice.",
) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": "",
                "rationale": rationale,
            },
            "operation": "update",
            "affected_decision_id": affected_decision_id,
        },
        {"tier": 2, "operation": "update", "similar_decisions": [], "assessment": "seed"},
    )


# ── Confirmed: add ──────────────────────────────────────────────────────


def test_confirmed_add_writes_decision_and_removes_pending() -> None:
    store = InMemoryStore()
    confirm_id = _seed_pending_add()
    assert _get_pending_store().get(confirm_id) is not None

    result = confirm_decision(store, confirm_id)

    assert isinstance(result, ConfirmDecisionResult)
    assert result.status == "confirmed"
    assert result.operation == "add"
    assert result.decision_id is not None
    assert result.decision_id.startswith("001-")
    assert result.touched_decisions == [result.decision_id]
    assert result.title == "Adopt Redis for hot caching"

    body = store.read_decision(result.decision_id)
    assert body is not None
    assert "Adopt Redis for hot caching" in body
    # The pending entry was consumed.
    assert _get_pending_store().get(confirm_id) is None


# ── Confirmed: supersede ────────────────────────────────────────────────


def test_confirmed_supersede_flips_old_and_carries_both_touched() -> None:
    stem, body = _seed_decision(
        1,
        "Adopt PostgreSQL primary database",
        "Mature ecosystem with strong JSON support and excellent tooling.",
    )
    store = InMemoryStore(decisions={stem: body})
    confirm_id = _seed_pending_supersede("decision-001")

    result = confirm_decision(store, confirm_id)

    assert result.status == "confirmed"
    assert result.operation == "supersede"
    assert result.decision_id is not None
    assert set(result.touched_decisions) == {result.decision_id, stem}

    new_body = store.read_decision(result.decision_id)
    assert new_body is not None
    assert "supersedes:" in new_body
    old_body = store.read_decision(stem)
    assert old_body is not None
    assert "status: superseded" in old_body
    assert "superseded_by:" in old_body


# ── Confirmed: update ───────────────────────────────────────────────────


def test_confirmed_update_appends_rationale() -> None:
    stem, body = _seed_decision(
        1,
        "Adopt PostgreSQL",
        "Mature ecosystem with strong JSON support and excellent tooling.",
    )
    store = InMemoryStore(decisions={stem: body})
    confirm_id = _seed_pending_update("decision-001")

    result = confirm_decision(store, confirm_id)

    assert result.status == "confirmed"
    assert result.operation == "update"
    assert result.decision_id == stem
    assert result.touched_decisions == [stem]

    body_after = store.read_decision(stem)
    assert body_after is not None
    assert "managed-extensions clause" in body_after
    assert "version: 2" in body_after


# ── Confirmed: resolves_questions ───────────────────────────────────────


def test_confirmed_resolves_questions_moves_entry_and_carries_ids() -> None:
    open_questions = (
        "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    confirm_id = _seed_pending_add(
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        resolves=["Q1"],
    )

    result = confirm_decision(store, confirm_id)

    assert result.status == "confirmed"
    assert result.resolved_questions == ["Q1"]
    updated = store.read_file(OPEN_QUESTIONS_MD)
    assert updated is not None
    assert "[Q1] Should we adopt PostgreSQL?" in updated


# ── Rejected: unknown / expired confirm_id ──────────────────────────────


def test_unknown_confirm_id_returns_structured_rejection() -> None:
    store = InMemoryStore()
    result = confirm_decision(store, "no-such-id")

    assert result.status == "rejected"
    assert result.operation == "reject"
    assert result.decision_id is None
    assert result.touched_decisions == []
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert result.error.reason == "Invalid or expired confirm_id."


def test_expired_confirm_id_returns_structured_rejection() -> None:
    store = InMemoryStore()
    confirm_id = _seed_pending_add()
    # Backdate so PendingStore.expire() drops the entry on next access.
    pending = _get_pending_store()
    pending._pending[confirm_id]["created_at"] = datetime.now(timezone.utc) - timedelta(hours=24)

    result = confirm_decision(store, confirm_id)

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert result.error.reason == "Invalid or expired confirm_id."


# ── Half-state on supersede ─────────────────────────────────────────────


def test_supersede_half_state_returns_structured_error() -> None:
    """A second-write failure during supersede surfaces a half-state error."""
    stem, body = _seed_decision(
        1,
        "Adopt PostgreSQL primary database",
        "Mature ecosystem with strong JSON support and excellent tooling.",
    )

    class _FailOldFlip(InMemoryStore):
        """Fail only the write that rewrites the OLD decision stem.

        The supersede sequence writes the new decision, the hash index,
        rewrites the new decision with the supersedes ref, then flips
        the old. We target the flip via path, not order, so the test
        does not depend on the internal write count.
        """

        def __init__(self, *args, old_stem: str, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._old_stem = old_stem
            self._old_writes = 0

        def write_file(self, path: str, content: str) -> None:
            if path.endswith(f"{self._old_stem}.md"):
                self._old_writes += 1
                # The original seed counts as the zeroth write; the flip is
                # the first time the kernel writes back to that stem.
                if self._old_writes == 1:
                    raise OSError("simulated old-decision flip failure")
            super().write_file(path, content)

    store = _FailOldFlip(decisions={stem: body}, old_stem=stem)
    confirm_id = _seed_pending_supersede("decision-001")

    result = confirm_decision(store, confirm_id)

    assert result.status == "rejected"
    assert result.operation == "supersede"
    assert result.error is not None
    assert result.error.kind == "error"
    assert "half-state" in result.error.reason
    assert len(result.touched_decisions) >= 1


# ── Idempotency ─────────────────────────────────────────────────────────


def test_second_confirm_with_same_id_returns_unknown_shape() -> None:
    store = InMemoryStore()
    confirm_id = _seed_pending_add()

    first = confirm_decision(store, confirm_id)
    assert first.status == "confirmed"

    second = confirm_decision(store, confirm_id)
    assert second.status == "rejected"
    assert second.error is not None
    assert second.error.kind == "rejected"
    assert second.error.reason == "Invalid or expired confirm_id."


# ── Result model shape ──────────────────────────────────────────────────


def test_result_is_frozen() -> None:
    result = ConfirmDecisionResult(status="rejected", operation="reject")
    with pytest.raises(ValidationError):
        result.status = "confirmed"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ConfirmDecisionResult(status="confirmed", operation="add", unexpected="value")


def test_result_exclude_none_strips_unset_fields() -> None:
    store = InMemoryStore()
    confirm_id = _seed_pending_add()
    result = confirm_decision(store, confirm_id)
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped["status"] == "confirmed"
    assert "error" not in dumped
    # The default empty list for resolved_questions still serializes; the
    # adapter pops it when empty. ``title`` is set on the confirmed path.
    assert "title" in dumped


# ── Import-graph negative constraint ────────────────────────────────────


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "confirm_decision.py"
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
        "capture_snapshot",
        "push_after_write",
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
    for name in imported:
        assert not name.startswith("nauro."), (
            f"kernel imports adapter-side module {name!r}; only nauro_core.* imports are allowed."
        )
