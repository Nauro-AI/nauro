"""Tests for the ``nauro journal`` command.

The command reads the store-local D455 event journal and emits every parseable
event as a JSON array on stdout, oldest first. A missing or empty journal emits
``[]``; a truncated final record is skipped by the tolerant reader.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.journal import (
    JOURNAL_DIR,
    JOURNAL_EVENTS_FILENAME,
    JournalEvent,
    OriginDescriptor,
    append_event,
)
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

_ORIGIN = OriginDescriptor(transport="cli", client_name="nauro-cli", client_version="1.8.0")


def _new_store(tmp_path, monkeypatch, name: str = "journalproj") -> Path:
    """Register and scaffold a project, chdir into its repo, return the store."""
    _pid, store = register_project_v2(name, [tmp_path])
    scaffold_project_store(name, store)
    monkeypatch.chdir(tmp_path)
    return store


def _event(operation: str, target: str, status: str = "committed") -> JournalEvent:
    return JournalEvent(
        operation=operation,
        target=target,
        status=status,  # type: ignore[arg-type]
        origin=_ORIGIN,
        payload_hash="0" * 64,
    )


def _events_file(store: Path) -> Path:
    return store / JOURNAL_DIR / JOURNAL_EVENTS_FILENAME


def test_missing_journal_emits_empty_array(tmp_path, monkeypatch):
    store = _new_store(tmp_path, monkeypatch)
    assert not _events_file(store).exists()

    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_empty_journal_file_emits_empty_array(tmp_path, monkeypatch):
    store = _new_store(tmp_path, monkeypatch)
    events_file = _events_file(store)
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_populated_journal_emits_events_in_append_order(tmp_path, monkeypatch):
    store = _new_store(tmp_path, monkeypatch)
    written = [
        _event("propose_decision", "decisions"),
        _event("flag_question", "open_questions"),
        _event("update_state", "state", status="rejected"),
    ]
    for event in written:
        append_event(store, event)

    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0

    emitted = json.loads(result.stdout)
    expected = [e.model_dump(mode="json", exclude_none=True) for e in written]
    assert emitted == expected


def test_truncated_final_record_is_skipped(tmp_path, monkeypatch):
    store = _new_store(tmp_path, monkeypatch)
    good = _event("propose_decision", "decisions")
    append_event(store, good)

    events_file = _events_file(store)
    partial = json.dumps({"operation": "flag_question", "target": "open_q"})[:20]
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(partial)

    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0

    emitted = json.loads(result.stdout)
    assert emitted == [good.model_dump(mode="json", exclude_none=True)]


def test_unknown_project_exits_1(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["journal", "--project", "does-not-exist"])
    assert result.exit_code == 1
