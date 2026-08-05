"""Cross-surface wiring for the ``base_commit`` provenance stamp.

The stdio wrapper derives the stamp from its ``cwd`` parameter and the CLI
from the process cwd; both land it as a trailing frontmatter key on newly
written decisions. Absence is fail-open — the key is omitted entirely, never
written as null — and the stamp is provenance, not payload: the journal's
payload hash must be invariant to it. No tool schema or CLI flag carries it.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import tools
from nauro.mcp.stdio_server import mcp, propose_decision
from nauro.store import repo_head
from nauro.store.journal import read_events
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_repo_head import _repo_with_commit

runner = CliRunner()

_LONG_RATIONALE = "Adopt the widget subsystem because it is measurably better here. " * 3


def _sole_decision_text(store_path: Path) -> str:
    """Text of the decision the test filed (the scaffold seeds 001 itself)."""
    files = list((store_path / "decisions").glob("*-adopt-widgets.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(tools, "_try_push", lambda _store_path: None)
    _pid, store_path = register_project_v2("stampproj", [tmp_path / "repo"])
    scaffold_project_store("stampproj", store_path)
    return store_path


def _propose_via_stdio(cwd: str | None) -> dict:
    kwargs = {} if cwd is None else {"cwd": cwd}
    return propose_decision(
        project_id="stampproj",
        title="Adopt widgets",
        rationale=_LONG_RATIONALE,
        **kwargs,
    )


# --- stdio wrapper: stamp derived from the cwd param -------------------------


class TestStdioStamp:
    def test_stamps_head_of_repo_enclosing_cwd(self, store: Path, tmp_path: Path):
        repo = tmp_path / "gitrepo"
        sha = _repo_with_commit(repo)
        result = _propose_via_stdio(str(repo))
        assert result["status"] == "confirmed"
        text = _sole_decision_text(store)
        assert f"base_commit: {sha}" in text
        # Trailing frontmatter key: inside the frontmatter block, after the
        # known keys.
        frontmatter = text.split("---\n")[1]
        assert frontmatter.rstrip().endswith(f"base_commit: {sha}")


# --- fail-open absence: the key is omitted, never null -----------------------


class TestFailOpenAbsence:
    def _assert_unstamped(self, store: Path, result: dict) -> None:
        assert result["status"] == "confirmed"
        assert "base_commit" not in _sole_decision_text(store)

    def test_cwd_omitted(self, store: Path):
        self._assert_unstamped(store, _propose_via_stdio(None))

    def test_cwd_outside_any_repo(self, store: Path, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        self._assert_unstamped(store, _propose_via_stdio(str(plain)))

    def test_git_absent(self, store: Path, tmp_path: Path, monkeypatch):
        repo = tmp_path / "gitrepo"
        _repo_with_commit(repo)

        def raise_missing(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(repo_head.subprocess, "run", raise_missing)
        self._assert_unstamped(store, _propose_via_stdio(str(repo)))

    def test_empty_repo(self, store: Path, tmp_path: Path):
        repo = tmp_path / "gitrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self._assert_unstamped(store, _propose_via_stdio(str(repo)))

    def test_timeout(self, store: Path, tmp_path: Path, monkeypatch):
        repo = tmp_path / "gitrepo"
        _repo_with_commit(repo)

        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=2)

        monkeypatch.setattr(repo_head.subprocess, "run", raise_timeout)
        self._assert_unstamped(store, _propose_via_stdio(str(repo)))


# --- CLI: stamp derived from the process cwd ---------------------------------


class TestCliStamp:
    def _project(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _pid, store = register_project_v2("clistamp", [repo])
        scaffold_project_store("clistamp", store)
        monkeypatch.chdir(repo)
        return repo, store

    def test_propose_decision_stamps_process_cwd_head(self, tmp_path: Path, monkeypatch):
        repo, store = self._project(tmp_path, monkeypatch)
        sha = _repo_with_commit(repo)
        result = runner.invoke(
            app,
            ["propose-decision", _LONG_RATIONALE, "--title", "Adopt widgets"],
        )
        assert result.exit_code == 0, result.output
        assert f"base_commit: {sha}" in _sole_decision_text(store)

    def test_propose_decision_outside_repo_is_unstamped(self, tmp_path: Path, monkeypatch):
        _repo, store = self._project(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["propose-decision", _LONG_RATIONALE, "--title", "Adopt widgets"],
        )
        assert result.exit_code == 0, result.output
        assert "base_commit" not in _sole_decision_text(store)


# --- journal: provenance, not payload ----------------------------------------


class TestJournal:
    def test_stamped_filing_emits_committed_event(self, store: Path, tmp_path: Path):
        repo = tmp_path / "gitrepo"
        _repo_with_commit(repo)
        result = _propose_via_stdio(str(repo))
        assert result["status"] == "confirmed"
        events = read_events(store)
        assert len(events) == 1
        event = events[0]
        assert event.operation == "propose_decision"
        assert event.status == "committed"
        assert event.decision_id == result["decision_id"]
        assert event.payload_hash

    def test_payload_hash_invariant_across_base_commit(self, store: Path):
        first = tools.tool_propose_decision(
            store,
            title="Adopt widgets",
            rationale=_LONG_RATIONALE,
            base_commit="a" * 40,
        )
        assert first["status"] == "confirmed"
        # Identical payload, different stamp: the duplicate rejection still
        # journals its payload hash, which must match the committed attempt.
        second = tools.tool_propose_decision(
            store,
            title="Adopt widgets",
            rationale=_LONG_RATIONALE,
            base_commit="b" * 40,
        )
        assert second["status"] == "rejected"
        events = read_events(store)
        assert len(events) == 2
        assert events[0].status == "committed"
        assert events[1].status == "rejected"
        assert events[0].payload_hash == events[1].payload_hash


# --- no new surface ----------------------------------------------------------


class TestNoNewSurface:
    def test_base_commit_absent_from_every_tool_schema(self):
        for tool in mcp._tool_manager.list_tools():
            props = tool.parameters.get("properties", {})
            assert "base_commit" not in props, tool.name

    def test_other_write_adapters_do_not_accept_base_commit(self):
        for adapter in (tools.tool_flag_question, tools.tool_update_state):
            assert "base_commit" not in inspect.signature(adapter).parameters

    def test_cli_has_no_base_commit_flag(self):
        result = runner.invoke(app, ["propose-decision", "--help"])
        assert result.exit_code == 0
        assert "base-commit" not in result.output
