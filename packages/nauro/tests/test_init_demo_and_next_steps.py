"""Tests for `nauro init --demo` behavior and the post-init next-step guidance.

* --demo prints the Repo: line, a cwd-config note, and a git-repo warning
  when the cwd is a git repo (and not otherwise).
* --demo reuses a single shared demo entry instead of duplicating it.
* The plain-init next-step points at `nauro setup`; --demo points at
  `nauro check-decision`.
* A repeat --demo in the same dir surfaces a reset-oriented message and not
  the generic --add-repo recovery line.

CWD and NAURO_HOME are isolated to tmp_path by autouse conftest fixtures.
"""

from __future__ import annotations

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry

runner = CliRunner()


# ── demo inside a real repo ─────────────────────────────────────────────────────


def test_demo_in_git_repo_warns(tmp_path, monkeypatch):
    """Inside a git repo, --demo names the repo, the cwd config, and warns."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    result = runner.invoke(app, ["init", "--demo"])
    assert result.exit_code == 0, result.output
    assert "Repo:" in result.output
    assert str(tmp_path.resolve()) in result.output
    assert ".nauro/config.json" in result.output
    assert "git repo" in result.output


def test_demo_in_non_git_dir_does_not_warn(tmp_path, monkeypatch):
    """Outside a git repo, the steering warning does not fire."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "--demo"])
    assert result.exit_code == 0, result.output
    assert "git repo" not in result.output
    assert (tmp_path / "AGENTS.md").is_file()
    assert "## Project: demo-project" in (tmp_path / "AGENTS.md").read_text()


# ── demo entry reuse ─────────────────────────────────────────────────────────────


def test_demo_reused_across_dirs(tmp_path, monkeypatch):
    """Two --demo runs in fresh dirs leave exactly one demo entry."""
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    first = runner.invoke(app, ["init", "--demo"])
    assert first.exit_code == 0, first.output

    monkeypatch.chdir(dir_b)
    second = runner.invoke(app, ["init", "--demo"])
    assert second.exit_code == 0, second.output
    assert "reusing it" in second.output

    assert len(registry.find_projects_by_name_v2("demo-project")) == 1
    assert (dir_a / "AGENTS.md").is_file()
    assert (dir_b / "AGENTS.md").is_file()


# ── next-step guidance ───────────────────────────────────────────────────────────


def test_plain_init_next_step_points_at_setup(tmp_path, monkeypatch):
    """Plain init guides the user to `nauro setup` next."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "plainproj"])
    assert result.exit_code == 0, result.output
    assert "setup" in result.output
    assert (
        "Then: run 'nauro sync' after project changes to refresh AGENTS.md and capture a snapshot"
    ) in result.output
    assert "capture the first snapshot" not in result.output


def test_demo_next_step_points_at_check_decision(tmp_path, monkeypatch):
    """--demo guides the user to try `nauro check-decision`."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--demo"])
    assert result.exit_code == 0, result.output
    assert "check-decision" in result.output
    assert "surface related prior decisions" in result.output
    assert "conflict check" not in result.output


# ── repeat-demo reset message ────────────────────────────────────────────────────


def test_repeat_demo_same_dir_offers_reset_not_add_repo(tmp_path, monkeypatch):
    """A second --demo in the same dir says to reset, never --add-repo."""
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "--demo"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["init", "--demo"])
    assert second.exit_code == 1, second.output
    assert "Demo already initialized" in second.output
    assert "--force to reset" in second.output
    assert "--add-repo" not in second.output
