"""Tests for ``nauro log --json`` and the ASCII table rules.

Three JSON modes: default emits the snapshot metadata list exactly as
``list_snapshots`` returns it; ``--decisions`` emits curated decision
rows; ``--full`` emits complete loaded snapshots including the files
map. Flag precedence mirrors the human surface exactly: ``--decisions``
wins over ``--full`` and ``--all`` only applies under ``--decisions``.
An empty store emits ``[]`` at exit 0.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from nauro_core.decision_model import Decision, DecisionStatus
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.store.snapshot import capture_snapshot, list_snapshots, load_snapshot
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import seed_decisions_into

runner = CliRunner()

SNAPSHOT_KEYS = {"version", "timestamp", "trigger", "trigger_detail", "token_count"}
DECISION_KEYS = {"num", "status", "version", "title", "superseded_by"}


def _setup_project(tmp_path, monkeypatch, *, scaffold=True) -> Path:
    _pid, store = register_project_v2("myproj", [tmp_path])
    if scaffold:
        scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def _seed_supersession_pair(store: Path) -> None:
    seed_decisions_into(
        store,
        Decision(
            date=date(2026, 1, 1),
            confidence="medium",
            num=1,
            title="Old storage choice",
            rationale="The original storage engine selection.",
            status=DecisionStatus.superseded,
            superseded_by="2",
        ),
        Decision(
            date=date(2026, 1, 2),
            confidence="medium",
            num=2,
            title="New storage choice",
            rationale="Replaces the original storage engine selection.",
            supersedes="1",
        ),
    )


class TestLogJsonSnapshots:
    def test_default_matches_list_snapshots(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch)
        capture_snapshot(store, trigger="first sync")
        capture_snapshot(store, trigger="second sync")

        result = runner.invoke(app, ["log", "--json"])
        assert result.exit_code == 0, result.output

        rows = json.loads(result.stdout)
        assert rows == list_snapshots(store)
        assert [set(row) for row in rows] == [SNAPSHOT_KEYS, SNAPSHOT_KEYS]
        assert [row["version"] for row in rows] == [2, 1]

    def test_limit_applies(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch)
        for i in range(5):
            capture_snapshot(store, trigger=f"snap-{i}")

        result = runner.invoke(app, ["log", "--json", "--limit", "2"])
        assert result.exit_code == 0, result.output
        assert [row["version"] for row in json.loads(result.stdout)] == [5, 4]

    def test_full_includes_files_map(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch)
        capture_snapshot(store, trigger="with files")

        result = runner.invoke(app, ["log", "--full", "--json"])
        assert result.exit_code == 0, result.output

        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0] == load_snapshot(store, 1)
        assert "project.md" in rows[0]["files"]

    def test_empty_store_emits_empty_list(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["log", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []


class TestLogJsonDecisions:
    def test_decisions_rows_filter_superseded(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch, scaffold=False)
        _seed_supersession_pair(store)

        result = runner.invoke(app, ["log", "--decisions", "--json"])
        assert result.exit_code == 0, result.output

        rows = json.loads(result.stdout)
        assert [set(row) for row in rows] == [DECISION_KEYS]
        assert rows[0] == {
            "num": 2,
            "status": "active",
            "version": 1,
            "title": "New storage choice",
            "superseded_by": None,
        }

    def test_decisions_all_includes_superseded(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch, scaffold=False)
        _seed_supersession_pair(store)

        result = runner.invoke(app, ["log", "--decisions", "--all", "--json"])
        assert result.exit_code == 0, result.output

        rows = json.loads(result.stdout)
        assert [row["num"] for row in rows] == [1, 2]
        assert rows[0]["status"] == "superseded"
        assert rows[0]["superseded_by"] == "2"

    def test_empty_store_emits_empty_list(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch, scaffold=False)

        result = runner.invoke(app, ["log", "--decisions", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []


class TestLogFlagPrecedence:
    def test_json_decisions_wins_over_full(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch, scaffold=False)
        _seed_supersession_pair(store)
        capture_snapshot(store, trigger="snap")

        result = runner.invoke(app, ["log", "--decisions", "--full", "--json"])
        assert result.exit_code == 0, result.output

        rows = json.loads(result.stdout)
        # Decision rows, not snapshot bodies: --full is silently ignored.
        assert [set(row) for row in rows] == [DECISION_KEYS]

    def test_json_all_without_decisions_is_ignored(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch)
        capture_snapshot(store, trigger="snap")

        with_all = runner.invoke(app, ["log", "--all", "--json"])
        without = runner.invoke(app, ["log", "--json"])
        assert with_all.exit_code == 0
        assert with_all.stdout == without.stdout

    def test_human_decisions_wins_over_full(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch, scaffold=False)
        _seed_supersession_pair(store)
        capture_snapshot(store, trigger="snap")

        result = runner.invoke(app, ["log", "--decisions", "--full"])
        assert result.exit_code == 0, result.output
        assert "New storage choice" in result.output
        assert "Snapshot v001" not in result.output

    def test_human_all_without_decisions_is_ignored(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch)
        capture_snapshot(store, trigger="snap")

        with_all = runner.invoke(app, ["log", "--all"])
        without = runner.invoke(app, ["log"])
        assert with_all.exit_code == 0
        assert with_all.stdout == without.stdout


class TestLogHumanAsciiRules:
    def test_summary_and_decision_tables_use_ascii_rules(self, tmp_path, monkeypatch):
        store = _setup_project(tmp_path, monkeypatch, scaffold=False)
        _seed_supersession_pair(store)
        capture_snapshot(store, trigger="first")
        capture_snapshot(store, trigger="second")

        summary = runner.invoke(app, ["log"])
        assert "-" * 60 in summary.output

        decisions = runner.invoke(app, ["log", "--decisions"])
        assert "-" * 70 in decisions.output

        full = runner.invoke(app, ["log", "--full"])
        assert "=" * 60 in full.output

        for output in (summary.output, decisions.output, full.output):
            assert "─" not in output
            assert "═" not in output
