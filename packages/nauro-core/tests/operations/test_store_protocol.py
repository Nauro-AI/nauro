"""Kernel tests for the ``Store`` protocol and the in-memory test impl."""

from __future__ import annotations

from nauro_core.operations import InMemoryStore, Store


def test_in_memory_store_satisfies_store_protocol() -> None:
    """``@runtime_checkable`` lets isinstance verify the surface at runtime."""
    store = InMemoryStore()
    assert isinstance(store, Store)


def test_read_file_returns_none_when_missing() -> None:
    store = InMemoryStore()
    assert store.read_file("does-not-exist.md") is None


def test_write_then_read_round_trip() -> None:
    store = InMemoryStore()
    store.write_file("state_current.md", "# state\n")
    assert store.read_file("state_current.md") == "# state\n"


def test_write_overwrites_existing_content() -> None:
    store = InMemoryStore(files={"state.md": "old"})
    store.write_file("state.md", "new")
    assert store.read_file("state.md") == "new"


def test_delete_file_removes_content() -> None:
    store = InMemoryStore(files={"state.md": "x"})
    store.delete_file("state.md")
    assert store.read_file("state.md") is None


def test_delete_missing_file_is_noop() -> None:
    store = InMemoryStore()
    # Must not raise.
    store.delete_file("never-existed.md")


def test_list_decisions_returns_sorted_stems() -> None:
    store = InMemoryStore(
        decisions={
            "003-third": "",
            "001-first": "",
            "002-second": "",
        }
    )
    assert store.list_decisions() == ["001-first", "002-second", "003-third"]


def test_list_decisions_empty_when_none_seeded() -> None:
    store = InMemoryStore()
    assert store.list_decisions() == []


def test_read_decision_round_trip() -> None:
    body = "---\ndate: 2026-01-01\nconfidence: medium\n---\n# 1. Title\n## Decision\nx\n"
    store = InMemoryStore(decisions={"001-title": body})
    assert store.read_decision("001-title") == body


def test_read_decision_returns_none_when_missing() -> None:
    store = InMemoryStore()
    assert store.read_decision("042-nope") is None


def test_read_decisions_round_trip_present_and_missing() -> None:
    store = InMemoryStore(
        decisions={
            "001-first": "body-1",
            "002-second": "body-2",
        }
    )
    bodies = store.read_decisions(["001-first", "002-second", "999-missing"])
    assert bodies == {
        "001-first": "body-1",
        "002-second": "body-2",
        "999-missing": None,
    }


def test_read_decisions_empty_stems_returns_empty_mapping() -> None:
    store = InMemoryStore(decisions={"001-first": "body-1"})
    assert store.read_decisions([]) == {}


def test_decisions_and_files_are_independent_namespaces() -> None:
    """Writing under a file path must not surface a decision stem, and vice versa."""
    store = InMemoryStore(decisions={"001-only-a-decision": "body"})
    store.write_file("001-only-a-decision", "file content")
    assert store.list_decisions() == ["001-only-a-decision"]
    assert store.read_decision("001-only-a-decision") == "body"
    assert store.read_file("001-only-a-decision") == "file content"
