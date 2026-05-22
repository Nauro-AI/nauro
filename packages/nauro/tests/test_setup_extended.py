"""Tests for the extended ``nauro setup`` (cursor + codex subcommands)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.commands.setup import (
    CHECK_HINT_LINE,
    _configure_codex,
    _configure_cursor_for_repo,
)
from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

runner = CliRunner()


def _strip_ansi(text: str) -> str:
    """Strip ANSI CSI escape sequences so substring checks survive Rich styling.

    Typer renders --help through Rich, which wraps each flag token in bold
    escapes when the runner detects a colour-capable terminal (CI with
    FORCE_COLOR=1 etc.). The escapes split ``--with-subagents`` into
    ``-\\x1b[0m\\x1b[1m-with\\x1b[0m\\x1b[1m-subagents``, breaking literal
    ``"--with-subagents" in output`` checks. NO_COLOR only suppresses colour,
    not bold/dim — stripping here is the only environment-independent fix.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\x1b" and i + 1 < n and text[i + 1] == "[":
            i += 2
            while i < n and text[i] != "m":
                i += 1
            i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# ─── nauro setup cursor ─────────────────────────────────────────────────────


def test_setup_cursor_writes_repo_mcp_json(tmp_path: Path, monkeypatch):
    """`nauro setup cursor` writes <repo>/.cursor/mcp.json for each project repo."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _pid, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "cursor"])
    assert result.exit_code == 0, result.output

    config_path = repo / ".cursor" / "mcp.json"
    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
    assert "command" in data["mcpServers"]["nauro"]


def test_setup_cursor_remove_clears_entry(tmp_path: Path, monkeypatch):
    """`nauro setup cursor --remove` deletes the nauro entry."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "cursor"])
    assert (repo / ".cursor" / "mcp.json").is_file()

    result = runner.invoke(app, ["setup", "cursor", "--remove"])
    assert result.exit_code == 0, result.output
    # Nauro was the only entry, so the now-empty config file is unlinked.
    assert not (repo / ".cursor" / "mcp.json").is_file()


def test_configure_cursor_preserves_other_mcp_servers(tmp_path: Path):
    """Adding nauro to .cursor/mcp.json must not clobber other servers."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    config = {"mcpServers": {"other": {"command": "other-cmd", "args": []}}}
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(config))

    _configure_cursor_for_repo(repo, remove=False)

    result = json.loads((repo / ".cursor" / "mcp.json").read_text())
    assert "other" in result["mcpServers"]
    assert "nauro" in result["mcpServers"]


def test_configure_cursor_remove_preserves_other_servers(tmp_path: Path):
    """`--remove` should drop only the nauro entry, not the rest."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    config = {
        "mcpServers": {
            "nauro": {"command": "nauro", "args": ["serve", "--stdio"]},
            "other": {"command": "other-cmd", "args": []},
        }
    }
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(config))

    _configure_cursor_for_repo(repo, remove=True)

    result = json.loads((repo / ".cursor" / "mcp.json").read_text())
    assert "other" in result["mcpServers"]
    assert "nauro" not in result["mcpServers"]


def test_configure_cursor_add_surfaces_parse_error_without_clobbering(tmp_path: Path):
    """Hand-edited / corrupt `.cursor/mcp.json` surfaces a parse error
    rather than crashing — same contract as `_configure_mcp` for `.mcp.json`."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    (repo / ".cursor" / "mcp.json").write_text("{not json")

    msg = _configure_cursor_for_repo(repo, remove=False)

    assert "could not parse .cursor/mcp.json" in msg
    assert (repo / ".cursor" / "mcp.json").read_text() == "{not json"


def test_configure_cursor_remove_surfaces_parse_error(tmp_path: Path):
    """Same parse-error contract on the `--remove` path."""
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    (repo / ".cursor" / "mcp.json").write_text("{not json")

    msg = _configure_cursor_for_repo(repo, remove=True)

    assert "could not parse .cursor/mcp.json" in msg


# ─── nauro setup codex ──────────────────────────────────────────────────────


def test_setup_codex_writes_config_toml(tmp_path: Path):
    """`_configure_codex` writes ``[mcp_servers.nauro]`` into the target TOML file."""
    config_path = tmp_path / ".codex" / "config.toml"

    msg = _configure_codex(remove=False, config_path=config_path)

    assert "wrote nauro" in msg
    assert config_path.is_file()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


def test_setup_codex_remove_clears_entry(tmp_path: Path):
    config_path = tmp_path / ".codex" / "config.toml"
    _configure_codex(remove=False, config_path=config_path)

    msg = _configure_codex(remove=True, config_path=config_path)

    assert "removed nauro" in msg
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert "mcp_servers" not in data or "nauro" not in data.get("mcp_servers", {})


def test_setup_codex_preserves_other_mcp_servers(tmp_path: Path):
    """Adding nauro must not clobber other [mcp_servers.<name>] entries."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('[mcp_servers.other]\ncommand = "other-cmd"\nargs = []\n')

    _configure_codex(remove=False, config_path=config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert "other" in data["mcp_servers"]
    assert "nauro" in data["mcp_servers"]


def test_setup_codex_no_op_when_remove_and_no_entry(tmp_path: Path):
    """`--remove` when no entry exists is idempotent and reports clearly."""
    config_path = tmp_path / ".codex" / "config.toml"
    msg = _configure_codex(remove=True, config_path=config_path)
    assert "no nauro entry to remove" in msg


def test_configure_codex_add_surfaces_parse_error(tmp_path: Path):
    """Hand-edited / corrupt `~/.codex/config.toml` surfaces a parse error
    rather than crashing — same contract as the JSON handlers."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("this is not = valid [toml")

    msg = _configure_codex(remove=False, config_path=config_path)

    assert "Codex: could not parse" in msg
    assert str(config_path) in msg
    # File left untouched.
    assert config_path.read_text() == "this is not = valid [toml"


def test_configure_codex_remove_surfaces_parse_error(tmp_path: Path):
    """Same parse-error contract on the `--remove` path."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("this is not = valid [toml")

    msg = _configure_codex(remove=True, config_path=config_path)

    assert "Codex: could not parse" in msg
    assert str(config_path) in msg


# ─── claude-code regression ──────────────────────────────────────────────────


def test_setup_claude_code_subcommand_unchanged(tmp_path: Path, monkeypatch):
    """The existing `setup claude-code` command stays callable."""
    monkeypatch.setenv("HOME", str(tmp_path))  # divert ~/.claude search
    repo = tmp_path / "myrepo"
    repo.mkdir()
    register_project_v2("myproj", [repo])
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "Configured Nauro" in result.output


def test_setup_top_level_help_lists_new_subcommands():
    """`nauro setup --help` should advertise cursor + codex alongside claude-code."""
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    assert "claude-code" in result.output
    assert "cursor" in result.output
    assert "codex" in result.output
    assert "all" in result.output


# ─── nauro setup all ────────────────────────────────────────────────────────


def test_setup_all_writes_claude_cursor_codex_configs(tmp_path: Path, monkeypatch):
    """`setup all` writes `.mcp.json`, `.cursor/mcp.json`, and `~/.codex/config.toml`
    plus the skill files across all three surfaces."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output

    # Claude Code: project-scope .mcp.json at the repo root (written
    # directly rather than via `claude mcp add`).
    assert (repo / ".mcp.json").is_file()
    mcp_data = json.loads((repo / ".mcp.json").read_text())
    assert mcp_data["mcpServers"]["nauro"]["args"] == ["serve", "--stdio"]
    # Cursor + Codex:
    assert (repo / ".cursor" / "mcp.json").is_file()
    assert (tmp_path / ".codex" / "config.toml").is_file()

    # Skill files (materialized):
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (repo / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
    assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()


def test_setup_all_remove_clears_everything(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "all"])
    result = runner.invoke(app, ["setup", "all", "--remove"])
    assert result.exit_code == 0, result.output

    # MCP configs gone:
    assert not (repo / ".mcp.json").exists()
    # Skill files gone:
    assert not (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert not (repo / ".cursor" / "rules" / "nauro-adopt.mdc").exists()
    assert not (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").exists()


def test_remove_skill_file_does_not_walk_above_base(tmp_path: Path):
    """``_remove_skill_file`` must stop at ``stop_above`` — never delete the surface root."""
    from nauro.cli.commands.setup import _remove_skill_file

    base = tmp_path / ".claude" / "skills"
    skill_dir = base / "nauro-adopt"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_text("body")

    _remove_skill_file(target, stop_above=base)

    assert not target.exists()
    assert not skill_dir.exists()  # subdir was empty → pruned
    assert base.is_dir()  # base preserved
    assert base.parent.is_dir()  # ~/.claude preserved
    assert base.parent.parent.is_dir()  # tmp_path preserved


def test_remove_skill_file_preserves_sibling_skills(tmp_path: Path):
    """If `~/.claude/skills/` has other skills, removing nauro must not touch them."""
    from nauro.cli.commands.setup import _remove_skill_file

    base = tmp_path / ".claude" / "skills"
    nauro_skill = base / "nauro-adopt" / "SKILL.md"
    other_skill = base / "user-other" / "SKILL.md"
    nauro_skill.parent.mkdir(parents=True)
    nauro_skill.write_text("nauro")
    other_skill.parent.mkdir(parents=True)
    other_skill.write_text("user-other")

    _remove_skill_file(nauro_skill, stop_above=base)

    assert not nauro_skill.exists()
    assert not nauro_skill.parent.exists()  # nauro subdir pruned
    assert other_skill.exists()  # other skill untouched
    assert base.is_dir()


# ─── multi-project remove gating ────────────────────────────────────────────


def test_remove_preserves_user_scope_when_other_projects_exist(tmp_path: Path, monkeypatch):
    """``setup all --remove`` for one project must not strip the user-scope
    Claude/Codex skills or the codex MCP entry while another nauro project
    remains in the registry — those resources are shared."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    _, store_a = register_project_v2("proj-a", [repo_a])
    scaffold_project_store("proj-a", store_a)
    _, store_b = register_project_v2("proj-b", [repo_b])
    scaffold_project_store("proj-b", store_b)

    monkeypatch.chdir(repo_a)
    runner.invoke(app, ["setup", "all", "--project", "proj-a"])
    monkeypatch.chdir(repo_b)
    runner.invoke(app, ["setup", "all", "--project", "proj-b"])

    monkeypatch.chdir(repo_a)
    result = runner.invoke(app, ["setup", "all", "--project", "proj-a", "--remove"])
    assert result.exit_code == 0, result.output

    # User-scope artifacts preserved — proj-b still depends on them.
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()

    codex_config = tmp_path / ".codex" / "config.toml"
    assert codex_config.is_file()
    with codex_config.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]

    # Per-repo wiring for proj-a still got torn down.
    assert not (repo_a / ".mcp.json").is_file()
    assert not (repo_a / ".cursor" / "mcp.json").is_file()
    # And proj-b's per-repo wiring stayed put.
    assert (repo_b / ".mcp.json").is_file()
    assert (repo_b / ".cursor" / "mcp.json").is_file()


def test_remove_clears_user_scope_when_last_project(tmp_path: Path, monkeypatch):
    """When removing the last project, the user-scope skill files and the
    codex MCP entry are fully cleared — nothing depends on them anymore."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("solo", [repo])
    scaffold_project_store("solo", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "all"])
    result = runner.invoke(app, ["setup", "all", "--remove"])
    assert result.exit_code == 0, result.output

    assert not (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert not (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").exists()

    codex_config = tmp_path / ".codex" / "config.toml"
    if codex_config.is_file():
        with codex_config.open("rb") as f:
            data = tomllib.load(f)
        assert "nauro" not in data.get("mcp_servers", {})


def test_standalone_codex_remove_preserves_when_projects_remain(tmp_path: Path, monkeypatch):
    """``nauro setup codex --remove`` must not strip ``~/.codex/config.toml``'s
    nauro entry while any projects remain in the registry. The standalone
    command is user-global; preservation guards still-active projects."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    register_project_v2("still-here", [repo])

    runner.invoke(app, ["setup", "codex"])
    codex_config = tmp_path / ".codex" / "config.toml"
    assert codex_config.is_file()

    result = runner.invoke(app, ["setup", "codex", "--remove"])
    assert result.exit_code == 0, result.output
    assert "preserved nauro entry" in result.output

    with codex_config.open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


# ─── nauro check-decision discoverability hint ──────────────────────────────


def test_setup_claude_code_advertises_check_decision(tmp_path: Path, monkeypatch):
    """`setup claude-code` success output points users at the L1 surface."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    register_project_v2("myproj", [repo])
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code"])
    assert result.exit_code == 0, result.output
    assert CHECK_HINT_LINE in result.output


def test_setup_claude_code_remove_does_not_advertise_check_decision(tmp_path: Path, monkeypatch):
    """The hint only fires on the add path — removal output stays minimal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    register_project_v2("myproj", [repo])
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code", "--remove"])
    assert result.exit_code == 0, result.output
    assert CHECK_HINT_LINE not in result.output


def test_setup_cursor_advertises_check_decision(tmp_path: Path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "cursor"])
    assert result.exit_code == 0, result.output
    assert CHECK_HINT_LINE in result.output


def test_setup_all_advertises_check_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output
    assert CHECK_HINT_LINE in result.output


def test_setup_codex_advertises_check_decision(tmp_path: Path, monkeypatch):
    """`setup codex` also advertises `nauro check-decision` — a Codex user
    benefits from knowing they can demo conflict-detection from the shell
    before opening a Codex session.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["setup", "codex"])
    assert result.exit_code == 0, result.output
    assert CHECK_HINT_LINE in result.output


# ─── nauro setup all --with-subagents ───────────────────────────────────────


def test_setup_all_with_subagents_installs_and_removes_files(tmp_path: Path, monkeypatch):
    """Round-trip: ``setup all --with-subagents`` writes nauro-*.md files;
    ``setup all --remove --with-subagents`` clears them when no other projects remain."""
    from nauro.agents import AGENT_NAMES, render_agent

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "all", "--with-subagents"])
    for name in AGENT_NAMES:
        target = tmp_path / ".claude" / "agents" / f"{name}.md"
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == render_agent("claude_code", name)

    result = runner.invoke(app, ["setup", "all", "--remove", "--with-subagents"])
    assert result.exit_code == 0, result.output
    for name in AGENT_NAMES:
        assert not (tmp_path / ".claude" / "agents" / f"{name}.md").exists()


def test_setup_all_with_subagents_remove_preserves_customized_files(tmp_path: Path, monkeypatch):
    """The remove path must leave locally-modified bundled files alone.

    Symmetric to the add path's preserve behavior: byte-equal files are
    unlinked, modified files stay so the user's edits aren't lost.
    """
    from nauro.agents import AGENT_NAMES

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "all", "--with-subagents"])

    target = tmp_path / ".claude" / "agents" / "nauro-planner.md"
    custom = "---\nname: nauro-planner\n---\n\nlocal tweak\n"
    target.write_text(custom, encoding="utf-8")

    result = runner.invoke(app, ["setup", "all", "--remove", "--with-subagents"])
    assert result.exit_code == 0, result.output

    # nauro-planner was modified → preserved
    assert target.read_text(encoding="utf-8") == custom
    # The other three matched the bundled content → unlinked
    for name in AGENT_NAMES:
        if name == "nauro-planner":
            continue
        assert not (tmp_path / ".claude" / "agents" / f"{name}.md").exists()
    assert "preserved" in result.output
    assert "locally modified" in result.output


def test_setup_all_default_does_not_install_subagents(tmp_path: Path, monkeypatch):
    """Off-by-default: ``setup all`` without the flag installs nothing under agents/."""
    from nauro.agents import AGENT_NAMES

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output
    for name in AGENT_NAMES:
        assert not (tmp_path / ".claude" / "agents" / f"{name}.md").exists()


def test_setup_all_help_lists_with_subagents():
    """``setup all --help`` advertises the new flag for discovery."""
    result = runner.invoke(app, ["setup", "all", "--help"])
    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "--with-subagents" in output
    assert "--force-overwrite" in output
    assert "--with-skills" in output


# ─── nauro setup all --with-skills ──────────────────────────────────────────


def test_setup_all_default_does_not_install_ship_task(tmp_path: Path, monkeypatch):
    """Off-by-default: ``setup all`` without ``--with-skills`` installs only /nauro-adopt."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output

    # nauro-adopt installed everywhere; nauro-ship-task absent everywhere.
    assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
    assert not (tmp_path / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md").exists()
    assert not (tmp_path / ".agents" / "skills" / "nauro-ship-task" / "SKILL.md").exists()
    assert not (repo / ".cursor" / "rules" / "nauro-ship-task.mdc").exists()


def test_setup_all_with_skills_installs_ship_task_everywhere(tmp_path: Path, monkeypatch):
    """``--with-skills`` round-trips: install writes, ``--remove --with-skills`` clears."""
    from nauro.skills import render_skill

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    install = runner.invoke(app, ["setup", "all", "--with-skills"])
    assert install.exit_code == 0, install.output

    claude = tmp_path / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md"
    codex = tmp_path / ".agents" / "skills" / "nauro-ship-task" / "SKILL.md"
    cursor = repo / ".cursor" / "rules" / "nauro-ship-task.mdc"
    assert claude.read_text(encoding="utf-8") == render_skill("claude_code", "nauro-ship-task")
    assert codex.read_text(encoding="utf-8") == render_skill("codex", "nauro-ship-task")
    assert cursor.read_text(encoding="utf-8") == render_skill("cursor", "nauro-ship-task")

    remove = runner.invoke(app, ["setup", "all", "--remove", "--with-skills"])
    assert remove.exit_code == 0, remove.output

    assert not claude.exists()
    assert not codex.exists()
    assert not cursor.exists()


def test_setup_all_with_skills_emits_notice_when_subagents_off(tmp_path: Path, monkeypatch):
    """The body references @nauro-* subagents; the user-facing add path warns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all", "--with-skills"])
    assert result.exit_code == 0, result.output
    assert "nauro-ship-task references the bundled @nauro-* subagents" in result.output


def test_setup_all_with_skills_and_subagents_suppresses_notice(tmp_path: Path, monkeypatch):
    """When both flags are passed, no notice — prerequisites met."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all", "--with-skills", "--with-subagents"])
    assert result.exit_code == 0, result.output
    assert "nauro-ship-task references the bundled @nauro-* subagents" not in result.output


def test_setup_all_remove_without_with_skills_leaves_ship_task_intact(tmp_path: Path, monkeypatch):
    """If the user installed ``--with-skills`` and removes without the flag,
    the opt-in skill files persist — the remove path mirrors the install path's
    name set so partial cleanups are explicit, not silent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)

    runner.invoke(app, ["setup", "all", "--with-skills"])
    claude = tmp_path / ".claude" / "skills" / "nauro-ship-task" / "SKILL.md"
    assert claude.is_file()

    remove = runner.invoke(app, ["setup", "all", "--remove"])  # no --with-skills
    assert remove.exit_code == 0, remove.output

    # nauro-adopt is cleared (the always-installed set), nauro-ship-task is not.
    assert not (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").exists()
    assert claude.is_file()
