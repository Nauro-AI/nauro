"""Kernel-level tests for ``operations.get_raw_file`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`GetRawFileResult` directly. Surface-level wiring tests live in
each transport's own suite. Path-traversal protection is an adapter-side
concern; ``InMemoryStore`` has no traversal concept and so it is not
exercised here.
"""

from __future__ import annotations

from nauro_core.operations import (
    GetRawFileResult,
    InMemoryStore,
    get_raw_file,
)


def test_returns_result_type() -> None:
    result = get_raw_file(InMemoryStore(), "project.md")
    assert isinstance(result, GetRawFileResult)


def test_empty_store_returns_not_found_error() -> None:
    result = get_raw_file(InMemoryStore(), "project.md")
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert result.error.reason == "File not found: project.md"


def test_existing_file_returns_content() -> None:
    body = "# Project\nSome body text.\n"
    store = InMemoryStore(files={"project.md": body})
    result = get_raw_file(store, "project.md")
    assert result.error is None
    assert result.content == body


def test_missing_path_reason_is_locked_format() -> None:
    """Miss-reason format is part of the surface contract."""
    result = get_raw_file(InMemoryStore(), "notes/missing.md")
    assert result.error is not None
    assert result.error.reason == "File not found: notes/missing.md"


def test_exclude_none_strips_unset_fields_on_hit() -> None:
    store = InMemoryStore(files={"project.md": "body"})
    result = get_raw_file(store, "project.md")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {"content": "body"}
    assert "error" not in dumped


def test_exclude_none_strips_unset_fields_on_miss() -> None:
    result = get_raw_file(InMemoryStore(), "project.md")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "error": {"kind": "error", "reason": "File not found: project.md"},
    }
    assert "content" not in dumped


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = get_raw_file(InMemoryStore(), "project.md")
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped
