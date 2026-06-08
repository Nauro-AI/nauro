"""Tests for the `nauro projects` command (list + rm).

`nauro projects` lists every registry entry. `nauro projects rm <id>` removes
a single entry behind a confirmation prompt (skipped with --yes) and leaves
the on-disk store directory intact — the documented recovery path when init
refuses to reuse an already-claimed repo.

CWD and NAURO_HOME are isolated to tmp_path by autouse conftest fixtures.
"""

from __future__ import annotations

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.registry import get_store_path_v2, register_project_v2

runner = CliRunner()


def _seed_two_projects(tmp_path):
    """Register two local projects with distinct repo dirs; return their ids."""
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    pid_a, _ = register_project_v2("proj-a", [repo_a])
    pid_b, _ = register_project_v2("proj-b", [repo_b])
    return pid_a, pid_b


def test_projects_lists_all_entries(tmp_path, monkeypatch):
    """`nauro projects` prints every id and name; exit 0."""
    pid_a, pid_b = _seed_two_projects(tmp_path)
    result = runner.invoke(app, ["projects"])
    assert result.exit_code == 0, result.output
    assert pid_a in result.output
    assert pid_b in result.output
    assert "proj-a" in result.output
    assert "proj-b" in result.output


def test_projects_rm_removes_entry_leaves_store(tmp_path, monkeypatch):
    """`rm <id> --yes` drops the entry, leaves the store, prints its path."""
    pid_a, pid_b = _seed_two_projects(tmp_path)
    store_path = get_store_path_v2(pid_a)
    assert store_path.is_dir()

    result = runner.invoke(app, ["projects", "rm", pid_a, "--yes"])
    assert result.exit_code == 0, result.output
    assert str(store_path) in result.output

    # Entry gone, store intact, other project untouched.
    assert registry.get_project_v2(pid_a) is None
    assert store_path.is_dir()
    assert registry.get_project_v2(pid_b) is not None


def test_projects_rm_missing_id_exits_1(tmp_path, monkeypatch):
    """`rm <missing-id>` exits 1 with a clear message."""
    _seed_two_projects(tmp_path)
    result = runner.invoke(app, ["projects", "rm", "01KMISSING00000000000000000", "--yes"])
    assert result.exit_code == 1, result.output
    assert "No project registered" in result.output


def test_projects_rm_declined_confirm_keeps_entry(tmp_path, monkeypatch):
    """Declining the confirmation prompt leaves the entry in place."""
    pid_a, _pid_b = _seed_two_projects(tmp_path)
    result = runner.invoke(app, ["projects", "rm", pid_a], input="n\n")
    # typer.confirm(abort=True) raises Abort → exit 1 on decline.
    assert result.exit_code == 1, result.output
    assert registry.get_project_v2(pid_a) is not None


# ── duplicate-name discoverability ──────────────────────────────────────────────


def test_projects_flags_duplicate_names(tmp_path, monkeypatch):
    """`nauro projects` warns when two entries share a name (separate stores)."""
    register_project_v2("dup", [tmp_path / "a"])
    register_project_v2("dup", [tmp_path / "b"])
    result = runner.invoke(app, ["projects"])
    assert result.exit_code == 0, result.output
    assert "share a name" in result.output
    assert "'dup'" in result.output


def test_projects_no_duplicate_warning_for_unique_names(tmp_path, monkeypatch):
    register_project_v2("alpha", [tmp_path / "a"])
    register_project_v2("beta", [tmp_path / "b"])
    result = runner.invoke(app, ["projects"])
    assert result.exit_code == 0, result.output
    assert "share a name" not in result.output


def test_status_warns_when_name_shared(tmp_path, monkeypatch):
    """`nauro status` flags another local project sharing the resolved name."""
    repo_a = tmp_path / "ra"
    repo_b = tmp_path / "rb"
    repo_a.mkdir()
    repo_b.mkdir()

    monkeypatch.chdir(repo_a)
    assert runner.invoke(app, ["init", "shared"]).exit_code == 0
    monkeypatch.chdir(repo_b)
    assert runner.invoke(app, ["init", "shared"]).exit_code == 0

    monkeypatch.chdir(repo_a)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "share the name" in result.output and "shared" in result.output
