"""Kernel-level tests for ``operations.get_context`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`GetContextResult` directly. Surface-level wiring tests live in
each transport's own suite. The kernel does not read snapshots, append
the ``Last synced`` trailer, or emit the ``NO_CONTEXT_YET`` sentinel —
those decorations belong to the transport adapter and are pinned by the
surface-layer parity tests instead.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    GetContextResult,
    InMemoryStore,
    get_context,
)


def _seeded_store() -> InMemoryStore:
    """Build a populated store covering every recognised context file."""
    body = format_decision(
        Decision(
            num=1,
            title="Adopt Postgres",
            rationale="ACID compliance trumps document flexibility for this workload.",
            confidence=DecisionConfidence.high,
            status=DecisionStatus.active,
            date=date(2026, 1, 1),
        )
    )
    return InMemoryStore(
        files={
            "project.md": "# Project\n\nGoal: build the thing.\n",
            "state_current.md": "# Current State\n\nShipping v1.\n",
            "state_history.md": "## 2026-04-01T10:00Z\n\nEarlier note.\n",
            "stack.md": "# Stack\n- **Python 3.11** — primary language\n",
            "open-questions.md": "# Open Questions\n- [Q1] Do we cache?\n",
        },
        decisions={"001-adopt-postgres": body},
    )


def test_returns_result_type() -> None:
    result = get_context(InMemoryStore(), 0)
    assert isinstance(result, GetContextResult)


def test_invalid_level_returns_rejected_error() -> None:
    result = get_context(InMemoryStore(), 7)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert result.error.reason == "Invalid level: 7"


def test_l0_on_empty_store_returns_empty_string() -> None:
    result = get_context(InMemoryStore(), 0)
    assert result.error is None
    assert result.content == ""


def test_l1_on_empty_store_returns_empty_string() -> None:
    result = get_context(InMemoryStore(), 1)
    assert result.error is None
    assert result.content == ""


def test_l2_on_empty_store_returns_empty_string() -> None:
    result = get_context(InMemoryStore(), 2)
    assert result.error is None
    assert result.content == ""


def test_l0_omits_project_md() -> None:
    """L0 deliberately drops project.md — AGENTS.md re-includes it."""
    result = get_context(_seeded_store(), 0)
    assert result.content is not None
    assert "Goal: build the thing." not in result.content
    # L0 still renders state, stack, and decisions.
    assert "Current State" in result.content
    assert "Python 3.11" in result.content
    assert "Adopt Postgres" in result.content


def test_l1_includes_project_md_and_full_decisions() -> None:
    result = get_context(_seeded_store(), 1)
    assert result.content is not None
    assert "Goal: build the thing." in result.content
    # L1 carries the full decision body, including rationale.
    assert "ACID compliance" in result.content


def test_l2_includes_state_history() -> None:
    result = get_context(_seeded_store(), 2)
    assert result.content is not None
    assert "Earlier note." in result.content
    assert "Adopt Postgres" in result.content


def test_legacy_state_md_falls_back_when_state_current_missing() -> None:
    """Pre-upgrade stores with only state.md still surface state content."""
    store = InMemoryStore(
        files={
            "state.md": "# State\n\n## Current\nLegacy current state.\n",
            "stack.md": "# Stack\n- Python\n",
        }
    )
    result = get_context(store, 1)
    assert result.content is not None
    assert "Legacy current state." in result.content


def test_empty_state_current_falls_back_to_legacy_state_md() -> None:
    """A migration placeholder (empty state_current.md) must not mask state.md.

    Pre-cutover the local reader used a truthy check on ``state_current.md``
    content, so a store left mid-migration with an empty placeholder still
    surfaced the populated legacy ``state.md`` body. The kernel preserves
    that behaviour — pinning it here so the fallback can't regress to
    ``is not None`` again.
    """
    store = InMemoryStore(
        files={
            "state_current.md": "",
            "state.md": "# State\n\n## Current\nLegacy body survives.\n",
            "stack.md": "# Stack\n- Python\n",
        }
    )
    for level in (0, 1, 2):
        result = get_context(store, level)
        assert result.content is not None, f"level {level} returned None content"
        assert "Legacy body survives." in result.content, (
            f"level {level} did not fall through to state.md: {result.content!r}"
        )


def test_exclude_none_strips_unset_fields_on_hit() -> None:
    result = get_context(_seeded_store(), 0)
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert "content" in dumped
    assert "error" not in dumped


def test_exclude_none_strips_unset_fields_on_miss() -> None:
    result = get_context(InMemoryStore(), -1)
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "error": {"kind": "rejected", "reason": "Invalid level: -1"},
    }
    assert "content" not in dumped


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = get_context(InMemoryStore(), 0)
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


def test_no_snapshot_diff_in_l2_content() -> None:
    """Snapshot reads are outside the locked Store protocol — the kernel
    never assembles the snapshot-diff trailer; that belongs to the
    transport adapter."""
    result = get_context(_seeded_store(), 2)
    assert result.content is not None
    assert "Snapshot Diff" not in result.content


def test_no_last_synced_trailer_in_l0_content() -> None:
    """The ``Last synced`` trailer is an adapter-side decoration.

    The state file may itself contain a ``**Last synced:**`` marker (the
    transport mines that line to render its own italic trailer); the kernel
    output passes the marker through as-is but never appends the italicised
    trailer line of its own.
    """
    store = InMemoryStore(
        files={
            "state_current.md": "# Current State\n\n**Last synced:** 2026-05-21\n",
            "stack.md": "# Stack\n- Python\n",
        }
    )
    result = get_context(store, 0)
    assert result.content is not None
    # The italic trailer ``*Last synced: <value>*`` is appended by the
    # transport adapter; the kernel content must not carry it.
    assert "\n\n*Last synced:" not in result.content
