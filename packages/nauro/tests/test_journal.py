"""Tests for the write-path provenance journal.

Covers the ``nauro.store.journal`` module (hashing, append/read, fail-open,
lock placement) and the ``@journaled`` wiring on the three write adapters
(committed vs rejected events, the no-event branches, and the fail-open
guarantee that a journaling defect never breaks a store write).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.mcp import tools
from nauro.mcp.tools import tool_flag_question, tool_propose_decision, tool_update_state
from nauro.store.journal import (
    JOURNAL_DIR,
    JOURNAL_EVENTS_FILENAME,
    JOURNAL_LOCK_NAME,
    JournalEvent,
    OriginDescriptor,
    append_event,
    payload_hash,
    read_events,
)
from nauro.store.registry import register_project_v2
from nauro.store.snapshot import capture_snapshot, load_snapshot
from nauro.store.store_lock import rmw_lock_path
from nauro.templates.scaffolds import scaffold_project_store

_ORIGIN = OriginDescriptor(transport="stdio-mcp", client_name="claude-code", client_version="1.2.3")

_LONG_RATIONALE = "Adopt the widget subsystem because it is measurably better here. " * 3


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Scaffolded project store with the best-effort cloud push disabled."""
    monkeypatch.setattr(tools, "_try_push", lambda _store_path: None)
    _pid, store_path = register_project_v2("journalproj", [tmp_path / "repo"])
    scaffold_project_store("journalproj", store_path)
    return store_path


def _events_file(store_path: Path) -> Path:
    return store_path / JOURNAL_DIR / JOURNAL_EVENTS_FILENAME


# --- payload hashing ---------------------------------------------------------


class TestPayloadHash:
    def test_key_order_independent(self):
        assert payload_hash({"a": 1, "b": 2, "c": [3, 4]}) == payload_hash(
            {"c": [3, 4], "b": 2, "a": 1}
        )

    def test_distinct_payloads_differ(self):
        assert payload_hash({"title": "x"}) != payload_hash({"title": "y"})

    def test_is_sha256_hex(self):
        digest = payload_hash({"k": "v"})
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


# --- append / read -----------------------------------------------------------


class TestAppendReadEvents:
    def test_one_append_is_one_line(self, store: Path):
        append_event(
            store,
            JournalEvent(
                operation="update_state",
                target="state_current.md",
                status="committed",
                origin=_ORIGIN,
                payload_hash=payload_hash({"delta": "x"}),
            ),
        )
        lines = _events_file(store).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "update_state"

    def test_read_missing_journal_is_empty(self, store: Path):
        assert read_events(store) == []

    def test_truncated_final_line_is_tolerated(self, store: Path):
        for i in range(2):
            append_event(
                store,
                JournalEvent(
                    operation="flag_question",
                    target="open-questions.md",
                    status="committed",
                    origin=_ORIGIN,
                    payload_hash=payload_hash({"question": f"q{i}"}),
                ),
            )
        # Simulate an interrupted append: a partial, unterminated final record.
        with _events_file(store).open("a", encoding="utf-8") as fh:
            fh.write('{"operation": "flag_question", "target": "open-que')
        events = read_events(store)
        assert len(events) == 2


# --- lock placement ----------------------------------------------------------


class TestJournalLockPath:
    def test_journal_lock_distinct_from_resource_locks(self, store: Path):
        journal_lock = store / JOURNAL_DIR / JOURNAL_LOCK_NAME
        decisions_lock = rmw_lock_path(store, "decisions", is_directory=True)
        questions_lock = rmw_lock_path(store, "open-questions.md")
        state_lock = rmw_lock_path(store, "state_current.md")
        assert journal_lock not in {decisions_lock, questions_lock, state_lock}
        # The journal lock lives inside its own directory, not the store root
        # or any resource directory.
        assert journal_lock.parent == store / JOURNAL_DIR


# --- fail-open ---------------------------------------------------------------


class TestFailOpen:
    def test_append_swallows_internal_error(self, store: Path):
        # An event whose serialization blows up must not surface from append.
        class Boom:
            def model_dump_json(self, **_kwargs):
                raise RuntimeError("serialize boom")

        # append_event must never raise, whatever it is handed.
        append_event(store, Boom())  # type: ignore[arg-type]
        assert not _events_file(store).exists()

    def test_write_survives_a_raising_journal_append(self, store: Path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("append boom")

        monkeypatch.setattr(tools, "append_event", boom)

        propose = tool_propose_decision(
            store, title="Ship the widget", rationale=_LONG_RATIONALE, origin=_ORIGIN
        )
        assert propose["status"] == "confirmed"

        flag = tool_flag_question(store, question="Should we ship?", origin=_ORIGIN)
        assert flag["status"] == "ok"

        update = tool_update_state(store, delta="Shipped the widget", origin=_ORIGIN)
        assert update["status"] == "ok"


# --- adapter wiring: committed / rejected / no-event -------------------------


class TestToolJournaling:
    def test_committed_decision_carries_hash_and_decision_id(self, store: Path):
        result = tool_propose_decision(
            store, title="Adopt widgets", rationale=_LONG_RATIONALE, origin=_ORIGIN
        )
        assert result["status"] == "confirmed"
        events = read_events(store)
        assert len(events) == 1
        event = events[0]
        assert event.operation == "propose_decision"
        assert event.status == "committed"
        assert event.decision_id == result["decision_id"]
        assert event.payload_hash
        assert event.origin is not None
        assert event.origin.transport == "stdio-mcp"

    def test_flag_question_commits_without_decision_id(self, store: Path):
        result = tool_flag_question(store, question="Do we cache reads?", origin=_ORIGIN)
        assert result["status"] == "ok"
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "flag_question"
        assert events[0].status == "committed"
        assert events[0].decision_id is None
        assert events[0].payload_hash

    def test_update_state_commits(self, store: Path):
        (store / "state_current.md").write_text("- prior state\n")
        result = tool_update_state(store, delta="Completed the migration", origin=_ORIGIN)
        assert result["status"] == "ok"
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "update_state"
        assert events[0].status == "committed"

    def test_update_state_noop_records_no_event(self, store: Path):
        # No state_current.md and no legacy state.md → kernel returns noop.
        (store / "state_current.md").unlink(missing_ok=True)
        result = tool_update_state(store, delta="Nothing to attach to", origin=_ORIGIN)
        assert result["status"] == "noop"
        assert read_events(store) == []

    def test_store_missing_records_no_event(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist"
        result = tool_propose_decision(
            missing, title="X", rationale=_LONG_RATIONALE, origin=_ORIGIN
        )
        assert result["status"] == "error"
        assert read_events(missing) == []

    def test_tier1_rejection_records_rejected_event(self, store: Path):
        oversize = "T" * 5000
        result = tool_propose_decision(
            store, title=oversize, rationale=_LONG_RATIONALE, origin=_ORIGIN
        )
        assert result["status"] == "rejected"
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "propose_decision"
        assert events[0].status == "rejected"
        assert events[0].decision_id is None
        assert events[0].payload_hash


# --- snapshot exclusion ------------------------------------------------------


class TestSnapshotExclusion:
    def test_snapshot_capture_omits_journal(self, store: Path):
        append_event(
            store,
            JournalEvent(
                operation="propose_decision",
                target="decisions",
                status="committed",
                origin=_ORIGIN,
                payload_hash=payload_hash({"title": "x"}),
            ),
        )
        version = capture_snapshot(store, trigger="test")
        snapshot = load_snapshot(store, version)
        assert not any(key.startswith(JOURNAL_DIR) for key in snapshot.get("files", {}))


# --- origin sanitization -----------------------------------------------------


class TestOriginSanitization:
    def test_control_characters_stripped_and_length_bounded(self):
        origin = OriginDescriptor(
            transport="stdio-mcp",
            client_name="cli\x00ent\nname",
            client_version="v" * 1000,
        )
        assert origin.client_name == "clientname"
        assert len(origin.client_version) == 256
