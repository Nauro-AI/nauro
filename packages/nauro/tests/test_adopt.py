"""Tests for ``nauro adopt``."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.agents import AGENT_NAMES, render_agent
from nauro.cli.main import app
from nauro.skills import load_adopt_body
from nauro.store.registry import find_projects_by_name_v2, register_project_v2

runner = CliRunner()


def _git_init(repo: Path) -> None:
    """Initialize a git repo. ``adopt`` refuses non-git directories."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _adopt_env(monkeypatch, tmp_path: Path) -> Path:
    """Set up an isolated NAURO_HOME + HOME for adopt tests."""
    monkeypatch.setenv("HOME", str(tmp_path))  # diverts ~/.claude, ~/.codex, ~/.agents
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
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


def test_adopt_from_home_is_refused(tmp_path: Path, monkeypatch):
    """A repo root whose .nauro/config.json is the global config is refused.

    The guard outranks the git and already-adopted checks: even as a git
    repo, the home directory must not read as an adoption, because the
    recovery hint there ("remove .nauro/config.json") would point at the
    file holding credentials and user-level settings.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "home"
    nauro_home = home / ".nauro"
    nauro_home.mkdir(parents=True)
    monkeypatch.setenv("NAURO_HOME", str(nauro_home))
    sentinel = '{"auth": {"access_token": "keep-me"}}\n'
    (nauro_home / "config.json").write_text(sentinel)
    _git_init(home)
    monkeypatch.chdir(home)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])

    assert result.exit_code == 1
    assert "global config" in result.output
    assert "already adopted" not in result.output.lower()
    assert (nauro_home / "config.json").read_text() == sentinel
    assert find_projects_by_name_v2("alpha") == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_adopt_refuses_symlinked_repo_config_before_registering(tmp_path: Path, monkeypatch):
    """A pre-planted symlink at .nauro/config.json aborts adoption cleanly.

    The refusal fires before ``register_project_v2``, so the registry gains
    no entry that would then need manual cleanup.
    """
    repo = _adopt_env(monkeypatch, tmp_path)
    (repo / ".nauro").mkdir()
    (repo / ".nauro" / "config.json").symlink_to(tmp_path / "attacker-target.json")

    result = runner.invoke(app, ["adopt", "--name", "alpha"])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    assert find_projects_by_name_v2("alpha") == []
    assert (repo / ".nauro" / "config.json").is_symlink()
    assert not (tmp_path / "attacker-target.json").exists()


def _planted_adoption(tmp_path: Path, monkeypatch) -> Path:
    """An un-adopted git repo whose config.json is a symlink to another
    repo's valid adoption config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    victim = tmp_path / "victim"
    victim.mkdir()
    _git_init(victim)
    monkeypatch.chdir(victim)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0

    attack = tmp_path / "attack"
    (attack / ".nauro").mkdir(parents=True)
    _git_init(attack)
    (attack / ".nauro" / "config.json").symlink_to(victim / ".nauro" / "config.json")
    monkeypatch.chdir(attack)
    return attack


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_adopt_with_skills_refuses_planted_config_link(tmp_path: Path, monkeypatch):
    """A link to another repo's valid config must not route into the
    already-adopted branch: the planted link is refused and no skill files
    are materialized."""
    attack = _planted_adoption(tmp_path, monkeypatch)

    result = runner.invoke(app, ["adopt", "--with-skills"])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    assert "already adopted" not in result.output.lower()
    assert not (tmp_path / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md").exists()
    assert not (attack / ".cursor").exists()
    assert (attack / ".nauro" / "config.json").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_adopt_plain_refuses_planted_config_link(tmp_path: Path, monkeypatch):
    """Plain adopt on the same planted link gets the symlink refusal, not the
    already-adopted hint that a real prior adoption would earn."""
    attack = _planted_adoption(tmp_path, monkeypatch)

    result = runner.invoke(app, ["adopt"])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    assert "already adopted" not in result.output.lower()
    assert (attack / ".nauro" / "config.json").is_symlink()


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
        assert not _codex_agent_path(tmp_path, n).exists()

    result = runner.invoke(app, ["adopt", "--with-subagents"])
    assert result.exit_code == 0, result.output
    assert "already adopted" not in result.output.lower() or "Installing" in result.output
    for n in AGENT_NAMES:
        assert _agent_path(tmp_path, n).is_file(), f"missing {n} after re-adopt --with-subagents"
        assert _codex_agent_path(tmp_path, n).is_file(), (
            f"missing Codex {n} after re-adopt --with-subagents"
        )


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
    _git_init(new_repo)
    monkeypatch.chdir(new_repo)

    result = runner.invoke(app, ["adopt"])  # infers name="alpha", collides
    assert result.exit_code == 1
    assert "A project named 'alpha' already exists" in result.output
    assert "--name <unique-name>" in result.output
    # Local association uses init --add-repo; cloud uses attach <pid>; cleanup
    # uses projects rm <pid>. The bogus `nauro link <pid>` suggestion is gone.
    assert "--add-repo" in result.output
    assert "nauro attach" in result.output
    assert "nauro projects rm" in result.output
    assert "nauro link " not in result.output


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
    _git_init(new_repo)
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


def test_adopt_warns_for_unignored_repo_config(tmp_path: Path, monkeypatch):
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--no-setup-and-skills"])
    assert result.exit_code == 0, result.output
    assert ".nauro/config.json is untracked and not git-ignored" in result.output
    assert "repo-local Nauro project config" in result.output


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


def _codex_agent_path(home: Path, agent: str) -> Path:
    return home / ".codex" / "agents" / f"{agent}.toml"


def test_adopt_default_does_not_install_subagents(tmp_path: Path, monkeypatch):
    """``nauro adopt`` without ``--with-subagents`` must not write any nauro-* agents."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    for name in AGENT_NAMES:
        assert not _agent_path(tmp_path, name).exists()
        assert not _codex_agent_path(tmp_path, name).exists()


def test_adopt_with_subagents_installs_all_four(tmp_path: Path, monkeypatch):
    """``--with-subagents`` materializes every bundled agent byte-equal to render_agent()."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-subagents"])
    assert result.exit_code == 0, result.output

    for name in AGENT_NAMES:
        claude = _agent_path(tmp_path, name)
        codex = _codex_agent_path(tmp_path, name)
        assert claude.read_text(encoding="utf-8") == render_agent("claude_code", name)
        assert codex.read_text(encoding="utf-8") == render_agent("codex", name)


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


def _loop_paths(home: Path, repo: Path) -> tuple[Path, Path, Path]:
    """Return the (claude, codex, cursor) target paths for the bundled
    /nauro-loop skill in an isolated HOME."""
    return (
        home / ".claude" / "skills" / "nauro-loop" / "SKILL.md",
        home / ".agents" / "skills" / "nauro-loop" / "SKILL.md",
        repo / ".cursor" / "rules" / "nauro-loop.mdc",
    )


def test_adopt_default_does_not_install_loop_skill(tmp_path: Path, monkeypatch):
    """Bare ``nauro adopt`` (no ``--with-skills``) leaves nauro-loop uninstalled."""
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    claude, codex, cursor = _loop_paths(tmp_path, repo)
    assert not claude.exists()
    assert not codex.exists()
    assert not cursor.exists()


def test_adopt_with_skills_installs_loop_across_surfaces(tmp_path: Path, monkeypatch):
    """``--with-skills`` materializes nauro-loop byte-equal to render_skill()."""
    from nauro.skills import render_skill

    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-skills"])
    assert result.exit_code == 0, result.output

    claude, codex, cursor = _loop_paths(tmp_path, repo)
    assert claude.is_file()
    assert codex.is_file()
    assert cursor.is_file()
    assert claude.read_text(encoding="utf-8") == render_skill("claude_code", "nauro-loop")
    assert codex.read_text(encoding="utf-8") == render_skill("codex", "nauro-loop")
    assert cursor.read_text(encoding="utf-8") == render_skill("cursor", "nauro-loop")


def test_adopt_print_prompt_conflicts_with_with_skills(tmp_path: Path, monkeypatch):
    """``--print-prompt`` plus ``--with-skills`` is rejected as mutually exclusive."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["adopt", "--print-prompt", "--with-skills"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ─── AGENTS.md generated at adopt time ──────────────────────────────────────


def test_adopt_writes_agents_md(tmp_path: Path, monkeypatch):
    """`adopt` produces AGENTS.md from the freshly-scaffolded store."""
    repo = _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    agents_md = repo / "AGENTS.md"
    assert agents_md.is_file()
    assert "## Project: alpha" in agents_md.read_text()


# ─── git precondition ───────────────────────────────────────────────────────


def test_adopt_refuses_non_git_directory(tmp_path: Path, monkeypatch):
    """`adopt` in a non-git directory exits 1, names git, and leaks no state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # NAURO_HOME is isolated to tmp_path by the autouse conftest fixture, so a
    # leaked registry entry would land there and be observable below.
    repo = tmp_path / "plain"
    repo.mkdir()  # no git init
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 1
    assert "git" in result.output.lower()

    # No per-repo config written.
    assert not (repo / ".nauro" / "config.json").exists()
    # No registry entry leaked.
    assert find_projects_by_name_v2("alpha") == []


def test_adopt_in_git_directory_fully_wires(tmp_path: Path, monkeypatch):
    """`adopt` in a git'd directory exits 0 and wires the surfaces (regression)."""
    repo = _adopt_env(monkeypatch, tmp_path)  # _adopt_env now git-inits

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output

    assert (repo / ".nauro" / "config.json").is_file()
    assert (repo / ".mcp.json").is_file()
    assert (repo / "AGENTS.md").is_file()
    assert len(find_projects_by_name_v2("alpha")) == 1


# ─── subagents connector-name surfacing ─────────────────────────────────────


def test_adopt_with_subagents_names_connector_requirement(tmp_path: Path, monkeypatch):
    """`adopt --with-subagents` names the required cloud connector name."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha", "--with-subagents"])
    assert result.exit_code == 0, result.output
    assert "name the remote MCP connector exactly `Nauro`" in result.output


def test_adopt_without_subagents_omits_connector_notice(tmp_path: Path, monkeypatch):
    """No connector-name notice when subagents are not installed."""
    _adopt_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["adopt", "--name", "alpha"])
    assert result.exit_code == 0, result.output
    assert "name the remote MCP connector exactly `Nauro`" not in result.output
