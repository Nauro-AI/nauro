"""Tests for ``nauro adopt``."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.agents import AGENT_NAMES, render_agent
from nauro.cli.main import app
from nauro.skills import load_adopt_body
from nauro.store.registry import find_projects_by_name_v2, register_project_v2

runner = CliRunner()


def _adopt_env(monkeypatch, tmp_path: Path) -> Path:
    """Set up an isolated NAURO_HOME + HOME for adopt tests."""
    monkeypatch.setenv("HOME", str(tmp_path))  # diverts ~/.claude, ~/.codex, ~/.agents
    repo = tmp_path / "myrepo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return repo


def test_adopt_creates_v2_project_and_writes_repo_config(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    # Per-repo config written.
    config_path = repo / ".nauro" / "config.json"
    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["name"] == "alpha"
    assert data["mode"] == "local"
    assert "id" in data

    # v2 registry has the entry.
    matches = find_projects_by_name_v2("alpha")
    assert len(matches) == 1
    pid, entry = matches[0]
    assert str(repo) in entry["repo_paths"]
    assert pid == data["id"]


def test_adopt_uses_repo_basename_when_name_omitted(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt"])
    assert result.exit_code == 0, result.output

    data = json.loads((repo / ".nauro" / "config.json").read_text())
    assert data["name"] == "myrepo"


def test_adopt_aborts_when_repo_already_adopted(tmp_path: Path, monkeypatch):
    _adopt_env(monkeypatch, tmp_path)
    runner.invoke(app, ["adopt", "--name", "alpha"])

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 1
    assert "already adopted" in result.output.lower()
    # The abort points at the flags that add artifacts, not only the
    # destructive "remove config.json" path.
    assert "--with-subagents" in result.output


def test_adopt_already_adopted_with_subagents_installs_instead_of_aborting(
    tmp_path: Path, monkeypatch
):
    """`adopt --with-subagents` on an already-adopted repo installs the bundled
    subagents rather than dead-ending on the already-adopted guard."""
    from nauro.agents import AGENT_NAMES

    _adopt_env(monkeypatch, tmp_path)
    first = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert first.exit_code == 0, first.output
    # Fresh adopt without the flag installs no subagents.
    for n in AGENT_NAMES:
        assert not _agent_path(tmp_path, n).exists()

    result = runner.invoke(app, ["adopt", "--with-subagents"])
    assert result.exit_code == 0, result.output
    assert "already adopted" not in result.output.lower() or "Installing" in result.output
    for n in AGENT_NAMES:
        assert _agent_path(tmp_path, n).is_file(), f"missing {n} after re-adopt --with-subagents"


def test_adopt_already_adopted_with_skills_installs_and_notices(tmp_path: Path, monkeypatch):
    """`adopt --with-skills` on an already-adopted repo installs the opt-in skill
    and emits the needs-subagents notice."""
    _adopt_env(monkeypatch, tmp_path)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0

    result = runner.invoke(app, ["adopt", "--with-skills"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md").is_file()


def test_adopt_aborts_on_same_name_collision(tmp_path: Path, monkeypatch):
    """Pre-check fires when the v2 registry has a same-name project at a different repo."""
    monkeypatch.setenv("HOME", str(tmp_path))
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    register_project_v2("alpha", [other_repo])

    new_repo = tmp_path / "alpha"
    new_repo.mkdir()
    monkeypatch.chdir(new_repo)

    result = runner.invoke(app, ["adopt"])  # infers name="alpha", collides
    assert result.exit_code == 1
    assert "A project named 'alpha' already exists" in result.output
    assert "--name <unique-name>" in result.output
    assert "nauro attach" in result.output
    assert "nauro link" in result.output


def test_collision_message_picks_first_repo_deterministically(tmp_path: Path, monkeypatch):
    """When the colliding project has multiple registered repos, the surfaced path is stable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_c = tmp_path / "repo-c"
    for r in (repo_a, repo_b, repo_c):
        r.mkdir()
    # Register colliding project with three repos in a known order.
    register_project_v2("alpha", [repo_a, repo_b, repo_c])

    new_repo = tmp_path / "alpha"
    new_repo.mkdir()
    monkeypatch.chdir(new_repo)

    # Run twice — same surfaced path each time (deterministic).
    out1 = runner.invoke(app, ["adopt"]).output
    out2 = runner.invoke(app, ["adopt"]).output
    assert out1 == out2
    # And the first registered repo (repo-a) is the one named.
    assert str(repo_a.resolve()) in out1


def test_adopt_print_prompt_outputs_canonical_body(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt"])
    assert result.exit_code == 0
    # Output equals the canonical body (Typer's runner adds a trailing newline; we used nl=False).
    assert result.output == load_adopt_body()


def test_adopt_print_prompt_conflicts_with_other_flags(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt", "--name", "x"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_adopt_no_setup_and_skills_skips_wiring(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--no-setup-and-skills"])
    assert result.exit_code == 0, result.output

    # Per-repo config still written...
    assert (repo / ".nauro" / "config.json").is_file()
    # ...but no skill files materialized.
    assert not (Path(tmp_path) / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert not (repo / ".cursor" / "rules" / "nauro-adopt.mdc").exists()
    assert not (Path(tmp_path) / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").exists()


def test_adopt_materializes_skills_across_surfaces(tmp_path: Path, monkeypatch):
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    # Claude Code: user-global
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    # Cursor: per-project (in target repo)
    assert (repo / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
    # Codex: user-global
    assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()


def test_adopt_aborts_on_nonexistent_repo(tmp_path: Path, monkeypatch):

    result = runner.invoke(app, ["adopt", "--repo", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_adopt_top_level_cli_help_lists_adopt():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "adopt" in result.output


# ─── --with-subagents ───────────────────────────────────────────────────────


def _agent_path(home: Path, agent: str) -> Path:
    return home / ".claude" / "agents" / f"{agent}.md"


def test_adopt_default_does_not_install_subagents(tmp_path: Path, monkeypatch):
    """``nauro adopt`` without ``--with-subagents`` must not write any nauro-* agents."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    for name in AGENT_NAMES:
        assert not _agent_path(tmp_path, name).exists()


def test_adopt_with_subagents_installs_all_four(tmp_path: Path, monkeypatch):
    """``--with-subagents`` materializes every bundled agent byte-equal to render_agent()."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-subagents"])
    assert result.exit_code == 0, result.output

    for name in AGENT_NAMES:
        target = _agent_path(tmp_path, name)
        assert target.is_file(), f"missing {target}"
        assert target.read_text(encoding="utf-8") == render_agent("claude_code", name)


def test_adopt_with_subagents_refreshes_differing_file_with_backup(tmp_path: Path, monkeypatch):
    """A differing ``nauro-planner.md`` is refreshed from the bundle; prior content goes to .bak.

    The ``nauro-*`` namespace is bundle-owned, so a stale earlier bundle (the
    common case) is replaced with the current one. The displaced content is
    recoverable from ``nauro-planner.md.bak`` for the rare hand-edit.
    """
    _adopt_env(monkeypatch, tmp_path)
    custom = "---\nname: nauro-planner\n---\n\ncustom body\n"
    target = _agent_path(tmp_path, "nauro-planner")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(custom, encoding="utf-8")

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-subagents"])
    assert result.exit_code == 0, result.output

    assert target.read_text(encoding="utf-8") == render_agent("claude_code", "nauro-planner")
    backup = target.parent / (target.name + ".bak")
    assert backup.read_text(encoding="utf-8") == custom
    assert "updated" in result.output
    assert ".bak" in result.output


def test_adopt_with_subagents_force_overwrite_skips_backup(tmp_path: Path, monkeypatch):
    """``--force-overwrite`` replaces a differing bundled agent in place, writing no .bak."""
    _adopt_env(monkeypatch, tmp_path)
    custom = "---\nname: nauro-planner\n---\n\ncustom body\n"
    target = _agent_path(tmp_path, "nauro-planner")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(custom, encoding="utf-8")

    result = runner.invoke(
        app, ["adopt", "--name", "alpha", "--with-subagents", "--force-overwrite"]
    )
    assert result.exit_code == 0, result.output

    assert target.read_text(encoding="utf-8") == render_agent("claude_code", "nauro-planner")
    assert not (target.parent / (target.name + ".bak")).exists()


def test_with_subagents_reinstall_is_idempotent(tmp_path: Path, monkeypatch):
    """A second install with no bundle change is a no-op — no churned .bak files.

    Driven via ``setup all`` (re-runnable) rather than ``adopt`` (which refuses
    an already-adopted repo); the materialize-agents code path is shared.
    """
    _adopt_env(monkeypatch, tmp_path)

    first = runner.invoke(app, ["adopt", "--name", "alpha", "--with-subagents"])
    assert first.exit_code == 0, first.output

    result = runner.invoke(app, ["setup", "all", "--with-subagents"])
    assert result.exit_code == 0, result.output

    target = _agent_path(tmp_path, "nauro-planner")
    assert "unchanged" in result.output
    assert not (target.parent / (target.name + ".bak")).exists()


def test_adopt_with_subagents_does_not_touch_user_authored_planner_md(tmp_path: Path, monkeypatch):
    """A user's ``~/.claude/agents/planner.md`` (no ``nauro-`` prefix) is never touched.

    This is the load-bearing test for namespacing: visitors who picked up
    ``planner`` from a different source — or who wrote their own — must
    not have their files replaced when they opt in to Nauro's bundle.
    """
    _adopt_env(monkeypatch, tmp_path)
    user_planner = tmp_path / ".claude" / "agents" / "planner.md"
    user_planner.parent.mkdir(parents=True, exist_ok=True)
    original = "---\nname: planner\n---\n\nuser's own planner body\n"
    user_planner.write_text(original, encoding="utf-8")

    result = runner.invoke(
        app, ["adopt", "--name", "alpha", "--with-subagents", "--force-overwrite"]
    )
    assert result.exit_code == 0, result.output

    assert user_planner.read_text(encoding="utf-8") == original


def test_adopt_print_prompt_conflicts_with_with_subagents(tmp_path: Path, monkeypatch):
    """``--print-prompt`` plus ``--with-subagents`` is rejected as mutually exclusive."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt", "--with-subagents"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ─── --with-skills ──────────────────────────────────────────────────────────


def _ship_task_paths(home: Path, repo: Path) -> tuple[Path, Path, Path]:
    """Return the (claude, codex, cursor) target paths for the bundled
    /nauro-ship-task skill in an isolated HOME."""
    return (
        home / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md",
        home / ".agents" / "skills" / "nauro-ship-task" / "SKILL.md",
        repo / ".cursor" / "rules" / "nauro-ship-task.mdc",
    )


def test_adopt_default_does_not_install_ship_task_skill(tmp_path: Path, monkeypatch):
    """Bare ``nauro adopt`` (no ``--with-skills``) leaves nauro-ship-task uninstalled."""
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    claude, codex, cursor = _ship_task_paths(tmp_path, repo)
    assert not claude.exists()
    assert not codex.exists()
    assert not cursor.exists()
    # And the always-installed /nauro-adopt skill is still present.
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()


def test_adopt_with_skills_installs_ship_task_across_surfaces(tmp_path: Path, monkeypatch):
    """``--with-skills`` materializes nauro-ship-task byte-equal to render_skill()."""
    from nauro.skills import render_skill

    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-skills"])
    assert result.exit_code == 0, result.output

    claude, codex, cursor = _ship_task_paths(tmp_path, repo)
    assert claude.is_file()
    assert codex.is_file()
    assert cursor.is_file()
    assert claude.read_text(encoding="utf-8") == render_skill("claude_code", "nauro-ship-task")
    assert codex.read_text(encoding="utf-8") == render_skill("codex", "nauro-ship-task")
    assert cursor.read_text(encoding="utf-8") == render_skill("cursor", "nauro-ship-task")


def test_adopt_with_skills_without_subagents_emits_notice(tmp_path: Path, monkeypatch):
    """The skill body references @nauro-* subagents; the install path warns."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-skills"])
    assert result.exit_code == 0, result.output
    assert "nauro-ship-task references the bundled @nauro-* subagents" in result.output


def test_adopt_with_skills_and_subagents_does_not_emit_notice(tmp_path: Path, monkeypatch):
    """When both flags are passed, the notice is suppressed — prerequisites met."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-skills", "--with-subagents"])
    assert result.exit_code == 0, result.output
    assert "nauro-ship-task references the bundled @nauro-* subagents" not in result.output


def test_adopt_remove_clears_ship_task_when_last_project(tmp_path: Path, monkeypatch):
    """``setup all --remove`` after a ``--with-skills`` install removes the new files too.

    There is no ``nauro adopt --remove`` flag; teardown goes through ``nauro
    setup all --remove``, which shares ``materialize_skills_*`` with adopt's
    install path. Verify the round-trip on the new bundled skill.
    """
    repo = _adopt_env(monkeypatch, tmp_path)

    install = runner.invoke(app, ["adopt", "--name", "alpha", "--with-skills"])
    assert install.exit_code == 0, install.output
    claude, codex, cursor = _ship_task_paths(tmp_path, repo)
    assert claude.is_file()

    remove = runner.invoke(app, ["setup", "all", "--remove", "--with-skills"])
    assert remove.exit_code == 0, remove.output

    assert not claude.exists()
    assert not codex.exists()
    assert not cursor.exists()


def test_adopt_print_prompt_conflicts_with_with_skills(tmp_path: Path, monkeypatch):
    """``--print-prompt`` plus ``--with-skills`` is rejected as mutually exclusive."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt", "--with-skills"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
