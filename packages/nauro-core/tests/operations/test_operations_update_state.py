"""Kernel-level tests for ``operations.update_state``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` so the
read/write/migrate plumbing exercises the locked Store protocol without
any filesystem dependency. Surface-level wiring (snapshot capture, push
hooks, length validation) lives in the consumer package; the kernel
must never reach into those primitives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from nauro_core.constants import (
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_LEGACY_FILENAME,
)
from nauro_core.operations import (
    InMemoryStore,
    UpdateStateResult,
    update_state,
)


def test_returns_result_type() -> None:
    result = update_state(InMemoryStore(), "anything")
    assert isinstance(result, UpdateStateResult)


def test_empty_store_returns_noop_and_writes_nothing() -> None:
    store = InMemoryStore()
    result = update_state(store, "Brand new state")
    assert result.status == "noop"
    assert result.warning is None
    assert result.error is None
    # No write happened — the kernel mirrors the pre-cutover writer's
    # early-return so the adapter can skip snapshot capture.
    assert store.read_file(STATE_CURRENT_FILENAME) is None
    assert store.read_file(STATE_HISTORY_FILENAME) is None


def test_current_only_non_overlapping_writes_new_body_and_appends_history() -> None:
    initial = "# Current State\n\n- Implemented OAuth login flow with PKCE\n"
    store = InMemoryStore(files={STATE_CURRENT_FILENAME: initial})
    result = update_state(store, "Reformatted README intro paragraph")
    assert result.status == "ok"
    assert result.warning is None
    new_current = store.read_file(STATE_CURRENT_FILENAME)
    assert new_current is not None
    assert "Reformatted README intro paragraph" in new_current
    assert new_current.startswith("# Current State")
    # The prior body archived into state_history.md verbatim.
    history = store.read_file(STATE_HISTORY_FILENAME)
    assert history is not None
    assert "Implemented OAuth login flow with PKCE" in history


def test_current_only_overlapping_delta_surfaces_warning() -> None:
    initial = "# Current State\n\n- Implemented OAuth login flow with PKCE\n"
    store = InMemoryStore(files={STATE_CURRENT_FILENAME: initial})
    result = update_state(store, "Implemented OAuth refresh logic with PKCE")
    assert result.status == "ok"
    assert result.warning is not None
    assert "keywords" in result.warning.lower()
    assert "Implemented OAuth login flow with PKCE" in result.warning


def test_legacy_state_only_migrates_to_state_current() -> None:
    legacy_body = "# State\n\n## Current\nLegacy content\n\n## History\n"
    store = InMemoryStore(files={STATE_LEGACY_FILENAME: legacy_body})
    result = update_state(store, "Post-upgrade task")
    assert result.status == "ok"
    # After migration the new state body is written.
    current = store.read_file(STATE_CURRENT_FILENAME)
    assert current is not None
    assert "Post-upgrade task" in current
    # The legacy body survives in state_history.md (migration archives the
    # pre-existing body once the migrated current is replaced).
    history = store.read_file(STATE_HISTORY_FILENAME)
    assert history is not None
    assert "Legacy content" in history


def test_history_accumulates_across_repeated_writes() -> None:
    initial = "# Current State\n\n- Task one\n"
    store = InMemoryStore(files={STATE_CURRENT_FILENAME: initial})
    update_state(store, "Task two")
    update_state(store, "Task three")
    current = store.read_file(STATE_CURRENT_FILENAME)
    assert current is not None
    assert "Task three" in current
    assert "Task two" not in current
    history = store.read_file(STATE_HISTORY_FILENAME)
    assert history is not None
    assert "Task one" in history
    assert "Task two" in history


def test_noop_result_exclude_none_strips_unset_fields() -> None:
    result = update_state(InMemoryStore(), "anything")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {"status": "noop"}


def test_ok_result_exclude_none_strips_unset_warning() -> None:
    initial = "# Current State\n\n- Task one\n"
    store = InMemoryStore(files={STATE_CURRENT_FILENAME: initial})
    result = update_state(store, "Reformat README")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {"status": "ok"}


def test_ok_result_with_warning_round_trips_in_dump() -> None:
    initial = "# Current State\n\n- Implemented OAuth login flow with PKCE\n"
    store = InMemoryStore(files={STATE_CURRENT_FILENAME: initial})
    result = update_state(store, "Implemented OAuth refresh logic with PKCE")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped["status"] == "ok"
    assert "warning" in dumped


def test_result_is_frozen() -> None:
    result = update_state(InMemoryStore(), "x")
    with pytest.raises(ValidationError):
        result.status = "ok"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        UpdateStateResult(status="ok", unexpected_field="value")


def test_store_field_absent_from_result_model_dump() -> None:
    result = update_state(InMemoryStore(), "x")
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# --- Import-graph negative constraint ---
#
# The kernel must not depend on snapshot capture, sync hooks, or
# pathlib — those are transport-side concerns that the locked Store
# protocol abstracts over. Pinning the constraint as an AST scan keeps
# the no-drift-by-construction guarantee from accidentally regressing
# through a casual import.


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "update_state.py"
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


def test_kernel_does_not_import_snapshot_or_sync_or_path() -> None:
    imported = _kernel_imports()
    forbidden_names = {
        "nauro.store.snapshot",
        "nauro.sync.hooks",
        "capture_snapshot",
        "push_after_write",
        "pull_before_session",
    }
    leaked = forbidden_names & imported
    assert not leaked, (
        f"kernel imports adapter-side primitives: {leaked}; "
        "those live on the transport, not in the kernel."
    )
    # pathlib stays out of the kernel — paths are strings through the Store.
    assert "pathlib" not in imported and "pathlib.Path" not in imported, (
        "kernel imports pathlib; the Store protocol abstracts paths as strings."
    )
