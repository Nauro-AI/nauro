"""Tests for nauro status command."""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

import nauro.cli.commands.status as status_mod
from nauro.cli import nauro_command
from nauro.cli._codex_hooks import _CODEX_HOOK_PROBE_ARGS
from nauro.cli.integrations import codex_config
from nauro.cli.integrations.agents import materialize_agents, materialize_agents_cursor_for_repo
from nauro.cli.integrations.skills import (
    materialize_skills_claude_code,
    materialize_skills_codex,
)
from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.agents_md import FOOTER_MARKER
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

# The real subprocess seam, captured before the autouse fixture stubs it.
_REAL_PROBE = nauro_command.probe_nauro_command


def _setup_project(tmp_path, monkeypatch, repos=None):
    """Register a project with the given repos (default: tmp_path itself).

    Also points the Codex-global probe at a path under tmp_path so a wired
    ~/.codex/config.toml on the developer's machine cannot leak into the
    detection assertions.
    """
    repos = repos if repos is not None else [tmp_path]
    _pid, store = register_project_v2("testproj", repos)
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(repos[0])
    monkeypatch.setattr(
        codex_config, "codex_config_path", lambda: tmp_path / "codex-home" / "config.toml"
    )
    return store


def _wire_repo_mcp(repo):
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"nauro": {"command": "nauro"}}}))


def _wire_codex_hooks(repo, *, command="nauro", events=("SessionStart", "SubagentStart")):
    hooks = {}
    entry = {
        "type": "command",
        "command": f"test -x {command} || exit 0; exec {command} hook codex-bootstrap",
    }
    for event in events:
        hooks[event] = [{"hooks": [entry]}]
    path = repo / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))


def _install_workflow_artifacts(repos: list[Path] | None = None) -> None:
    materialize_skills_claude_code(remove=False, with_skills=True)
    materialize_skills_codex(remove=False, with_skills=True)
    materialize_agents("claude_code", remove=False)
    materialize_agents("codex", remove=False)
    for repo in repos if repos is not None else [Path.cwd()]:
        materialize_agents_cursor_for_repo(repo, remove=False)


def test_status_mcp_and_agents_inactive_when_nothing_wired(tmp_path, monkeypatch):
    """No MCP config anywhere and no generated AGENTS.md → both rows inactive."""
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           inactive - run 'nauro setup all'" in result.output
    assert "Codex hooks   inactive - run 'nauro setup codex --with-hooks'" in result.output
    assert (
        "Skills        inactive - run 'nauro setup all' "
        "(--with-skills adds the opt-in skills)" in result.output
    )
    assert (
        "Workflow      not installed (opt-in) - "
        "'nauro setup all --with-subagents' adds the workflow agents" in result.output
    )
    assert "AGENTS.md     inactive - run 'nauro sync'" in result.output
    assert "Decisions:" in result.output


def test_status_reports_current_skills_and_agents_on_all_surfaces(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Skills        active (Claude 5/5; Codex 5/5)" in result.output
    assert "Workflow      active (Claude 4/4; Cursor 4/4; Codex 4/4)" in result.output


def test_status_aggregates_cursor_agents_across_registered_repos(tmp_path, monkeypatch):
    repo_one = tmp_path / "repo-one"
    repo_two = tmp_path / "repo-two"
    repo_one.mkdir()
    repo_two.mkdir()
    _setup_project(tmp_path, monkeypatch, [repo_one, repo_two])
    _install_workflow_artifacts([repo_one])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert (
        "Workflow      partial (Claude 4/4; Cursor 4/8; Codex 4/4) - "
        "run 'nauro setup all --with-subagents'" in result.output
    )


def test_status_reports_stale_cursor_agent(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()
    (tmp_path / ".cursor" / "agents" / "nauro-planner.md").write_text("stale\n", encoding="utf-8")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert (
        "Workflow      BROKEN - Claude 4/4; Cursor 3/4; Codex 4/4; installed Nauro "
        "agent files differ from this release; run 'nauro setup all --with-subagents'"
        in result.output
    )


def test_status_reports_stale_skill_and_legacy_codex_copy(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()
    home = tmp_path / "home"
    (home / ".agents" / "skills" / "nauro-ship-task" / "SKILL.md").write_text(
        "stale\n", encoding="utf-8"
    )
    legacy = home / ".codex" / "skills" / "nauro-ship-task" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy\n", encoding="utf-8")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Skills        BROKEN" in result.output
    assert "1 legacy Nauro skill copy under ~/.codex/skills" in result.output
    assert "migrate with 'nauro setup all --with-skills' or remove manually" in result.output


def test_status_reports_stale_skill_without_legacy_copy(tmp_path, monkeypatch):
    """A differing installed skill file alone → BROKEN with the refresh remedy."""
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()
    home = tmp_path / "home"
    (home / ".agents" / "skills" / "nauro-ship-task" / "SKILL.md").write_text(
        "stale\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Skills        BROKEN - Claude 5/5; Codex 4/5" in result.output
    assert "differ from this release; run 'nauro setup all --with-skills'" in result.output


def test_status_bare_adopt_states_optin_absence_without_degrading(tmp_path, monkeypatch):
    """Core skill installed, no opt-ins, no agents → active row + stated choice."""
    _setup_project(tmp_path, monkeypatch)
    materialize_skills_claude_code(remove=False, with_skills=False)
    materialize_skills_codex(remove=False, with_skills=False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert (
        "Skills        active (core installed; opt-in skills not installed - "
        "'nauro setup all --with-skills' adds them)" in result.output
    )
    assert (
        "Workflow      not installed (opt-in) - "
        "'nauro setup all --with-subagents' adds the workflow agents" in result.output
    )


def test_status_partial_optin_skills_prompt_completion(tmp_path, monkeypatch):
    """Opt-in skills on one surface only → partial with the --with-skills remedy."""
    _setup_project(tmp_path, monkeypatch)
    materialize_skills_claude_code(remove=False, with_skills=True)
    materialize_skills_codex(remove=False, with_skills=False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert (
        "Skills        partial (Claude 5/5; Codex 1/5) - "
        "run 'nauro setup all --with-skills'" in result.output
    )


def test_status_prior_release_skills_prompt_refresh_on_both_surfaces(tmp_path, monkeypatch):
    """A prior four-skill install is partial on both surfaces until refreshed."""
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "nauro-interview" / "SKILL.md").unlink()
    (home / ".agents" / "skills" / "nauro-interview" / "SKILL.md").unlink()

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert (
        "Skills        partial (Claude 4/5; Codex 4/5) - "
        "run 'nauro setup all --with-skills'" in result.output
    )


def test_status_mcp_partial_repo_wiring(tmp_path, monkeypatch):
    """One of two associated repos wired via .mcp.json → active (1/2)."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _setup_project(tmp_path, monkeypatch, repos=[repo1, repo2])
    _wire_repo_mcp(repo1)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           active (wired in 1/2 repos)" in result.output


def test_status_mcp_cursor_wiring_counts(tmp_path, monkeypatch):
    """A nauro entry in .cursor/mcp.json counts as repo wiring."""
    _setup_project(tmp_path, monkeypatch)
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": "nauro"}}})
    )

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           active (wired in 1/1 repos)" in result.output


def test_status_mcp_codex_global_only(tmp_path, monkeypatch):
    """No repo wiring but a nauro entry in the Codex global config → active."""
    _setup_project(tmp_path, monkeypatch)
    codex_config = tmp_path / "codex-home" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text('[mcp_servers.nauro]\ncommand = "nauro"\nargs = ["serve", "--stdio"]\n')

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           active (wired in 0/1 repos; Codex global)" in result.output


def test_status_agents_md_active_with_footer(tmp_path, monkeypatch):
    """An AGENTS.md carrying the generation footer counts as generated."""
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").write_text(f"# AGENTS.md\n\npayload\n\n{FOOTER_MARKER}https://x)\n")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "AGENTS.md     active (1/1 repos)" in result.output


def test_status_agents_md_without_footer_counts_not_generated(tmp_path, monkeypatch):
    """A hand-written AGENTS.md (no Nauro footer) does not count as generated."""
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n\nHand-written project notes.\n")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "AGENTS.md     inactive - run 'nauro sync'" in result.output


def test_status_unparseable_mcp_configs_render_unknown_not_unwired(tmp_path, monkeypatch):
    """A wiring config that exists but cannot be parsed is reported as unknown with its
    path; the setup remedy would be wrong advice. Status still exits 0."""
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / ".mcp.json").write_text("{not valid json")
    codex_config = tmp_path / "codex-home" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text("mcp_servers = not-toml [")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert f"MCP           unknown - could not read {tmp_path / '.mcp.json'}" in result.output
    assert "(+1 more)" in result.output
    assert "MCP           inactive" not in result.output


def test_status_unreadable_agents_md_is_unknown_not_hand_written(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").mkdir()

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert f"AGENTS.md     unknown - could not read {tmp_path / 'AGENTS.md'}" in result.output
    assert "AGENTS.md     inactive" not in result.output


def test_status_unreadable_codex_hooks_is_unknown_not_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b"\xff\xfe not utf-8")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert f"Codex hooks   unknown - could not read {hooks}" in result.output


def test_status_unreadable_skill_file_marks_skills_and_workflow_unknown(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _install_workflow_artifacts()
    skill = Path.home() / ".claude" / "skills" / "nauro-adopt" / "SKILL.md"
    skill.unlink()
    skill.mkdir()

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert f"Skills        unknown - could not read {skill}" in result.output
    assert "Workflow      active" in result.output


def test_status_unparseable_codex_hooks_is_unknown_not_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{not valid json")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert f"Codex hooks   unknown - could not read {hooks}: invalid JSON" in result.output


def test_status_keeps_broken_over_unknown_and_names_the_unreadable_file(tmp_path, monkeypatch):
    """A dead recorded command observed in one repo is not hidden by an unreadable config
    in another: the row stays BROKEN with the remedy and names the file it skipped."""
    wired = tmp_path / "wired"
    unreadable = tmp_path / "unreadable"
    wired.mkdir()
    unreadable.mkdir()
    _setup_project(tmp_path, monkeypatch, repos=[wired, unreadable])
    _wire_repo_mcp(wired)
    (unreadable / ".mcp.json").write_bytes(b"\xff\xfe")
    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda cmd, **kwargs: False)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           BROKEN - wired in 1/2 repos but the recorded command won't run; " in (
        result.output
    )
    assert f"re-run 'nauro setup all'; could not read {unreadable / '.mcp.json'}" in result.output


def test_status_shows_store_path(tmp_path, monkeypatch):
    """`nauro status` surfaces the absolute store path.

    The store lives at ~/.nauro/projects/<id>/ — outside any repo — and no other
    command prints it. An agent following the nauro-context skill needs it to
    resolve where to write context/<slug>.md.
    """
    store = _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Store:" in result.output
    assert str(store) in result.output


def test_status_sync_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Sync          inactive" in result.output


# ── MCP liveness probe ──────────────────────────────────────────────────────


def test_status_mcp_broken_when_recorded_command_dead(tmp_path, monkeypatch):
    """Wired but the recorded command fails the liveness probe → BROKEN, exit 0."""
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda cmd, **kwargs: False)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           BROKEN" in result.output
    assert "won't run" in result.output
    assert "re-run 'nauro setup all'" in result.output


def test_status_no_probe_skips_liveness(tmp_path, monkeypatch):
    """`--no-probe` reports presence only and never calls the probe."""
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
    )

    result = runner.invoke(app, ["status", "--no-probe"])
    assert result.exit_code == 0
    assert calls == []
    assert "MCP           active (wired in 1/1 repos)" in result.output


def test_status_codex_hooks_configured_when_both_events_are_wired(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   configured (wired in 1/1 repos; command healthy)" in result.output


def test_status_codex_hooks_no_probe_reports_configured(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command,
        "probe_nauro_command",
        lambda command, **kwargs: calls.append(command) or True,
    )

    result = runner.invoke(app, ["status", "--no-probe"])

    assert result.exit_code == 0
    assert calls == []
    assert "Codex hooks   configured (wired in 1/1 repos; liveness not probed)" in result.output


def test_status_codex_hooks_broken_when_command_is_dead(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path, command="/gone/nauro")
    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda command, **kwargs: False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   BROKEN" in result.output
    assert "recorded command won't run" in result.output


def test_status_codex_hooks_broken_when_event_is_missing(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path, events=("SessionStart",))
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command,
        "probe_nauro_command",
        lambda command, **kwargs: calls.append(command) or True,
    )

    result = runner.invoke(app, ["status", "--no-probe"])

    assert result.exit_code == 0
    assert calls == []
    assert "Codex hooks   BROKEN" in result.output
    assert "lifecycle wiring is incomplete" in result.output


def test_status_probes_shared_mcp_and_hook_command_for_each_purpose(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    _wire_codex_hooks(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        nauro_command,
        "probe_nauro_command",
        lambda command, **kwargs: calls.append((command, kwargs["args"])) or True,
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert calls == [("nauro", ("--version",)), ("nauro", _CODEX_HOOK_PROBE_ARGS)]
    assert "MCP           active" in result.output
    assert "Codex hooks   configured" in result.output


def test_status_shared_command_can_have_healthy_mcp_and_broken_hooks(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    _wire_codex_hooks(tmp_path)
    monkeypatch.setattr(
        nauro_command,
        "probe_nauro_command",
        lambda _command, **kwargs: kwargs["args"] == ("--version",),
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "MCP           active" in result.output
    assert "Codex hooks   BROKEN" in result.output


def test_status_shared_command_can_have_broken_mcp_and_healthy_hooks(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    _wire_codex_hooks(tmp_path)
    monkeypatch.setattr(
        nauro_command,
        "probe_nauro_command",
        lambda _command, **kwargs: kwargs["args"] == _CODEX_HOOK_PROBE_ARGS,
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "MCP           BROKEN" in result.output
    assert "Codex hooks   configured" in result.output


def test_status_uses_windows_hook_override_on_windows(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    hooks = {}
    entry = {
        "type": "command",
        "command": "load-posix-context",
        "commandWindows": (
            'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "'
            "if (Get-Command 'C:/Program Files/Nauro/nauro.exe' "
            "-ErrorAction SilentlyContinue) { & 'C:/Program Files/Nauro/nauro.exe' "
            'hook codex-bootstrap }; exit 0"'
        ),
    }
    for event in ("SessionStart", "SubagentStart"):
        hooks[event] = [{"hooks": [entry]}]
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    monkeypatch.setattr(status_mod, "_is_windows", lambda: True)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   configured" in result.output


def test_status_ignores_windows_only_nauro_override_on_posix(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    hooks = {}
    entry = {
        "type": "command",
        "command": "load-posix-context",
        "commandWindows": "powershell.exe -Command \"& 'nauro' hook codex-bootstrap\"",
    }
    for event in ("SessionStart", "SubagentStart"):
        hooks[event] = [{"hooks": [entry]}]
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    monkeypatch.setattr(status_mod, "_is_windows", lambda: False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   inactive" in result.output


def test_status_treats_empty_windows_override_as_inactive(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    hooks = {}
    entry = {
        "type": "command",
        "command": "nauro hook codex-bootstrap",
        "commandWindows": "",
    }
    for event in ("SessionStart", "SubagentStart"):
        hooks[event] = [{"hooks": [entry]}]
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    monkeypatch.setattr(status_mod, "_is_windows", lambda: True)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   inactive" in result.output


def test_status_never_executes_a_non_nauro_recorded_command(tmp_path, monkeypatch):
    """A repo's own .mcp.json / .codex/hooks.json command is reported, never run."""
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": "./setup-helper.sh"}}})
    )
    _wire_codex_hooks(tmp_path, command="./setup-helper.sh")
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert calls == []
    assert (
        "MCP           active (wired in 1/1 repos; './setup-helper.sh' is not a nauro install, "
        "not probed)"
    ) in result.output
    assert (
        "Codex hooks   configured (wired in 1/1 repos; './setup-helper.sh' is not a nauro "
        "install, not probed)"
    ) in result.output


def test_status_leaves_a_repo_shipped_script_unexecuted_end_to_end(tmp_path, monkeypatch):
    """With the real subprocess seam in place, a cloned repo's script still never runs."""
    _setup_project(tmp_path, monkeypatch)
    marker = tmp_path / "executed"
    script = tmp_path / "setup-helper.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    script.chmod(0o755)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": "./setup-helper.sh"}}})
    )
    monkeypatch.setattr(nauro_command, "probe_nauro_command", _REAL_PROBE)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert not marker.exists()
    assert "not probed" in result.output


def test_status_does_not_probe_nauro_tracked_inside_the_repo(tmp_path, monkeypatch):
    """An absolute path to a git-tracked file is the repo author's program, not this machine's."""
    _setup_project(tmp_path, monkeypatch)
    shipped = tmp_path / "tools" / "nauro"
    shipped.parent.mkdir()
    shipped.write_text("#!/bin/sh\nexit 0\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "tools/nauro"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ship"], cwd=tmp_path, check=True)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": str(shipped)}}})
    )
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert calls == []
    assert "is not a nauro install, not probed" in result.output


def test_status_still_probes_an_untracked_project_venv_nauro(tmp_path, monkeypatch):
    """The fragile project-venv install keeps its liveness row."""
    _setup_project(tmp_path, monkeypatch)
    venv_nauro = tmp_path / ".venv" / "bin" / "nauro"
    venv_nauro.parent.mkdir(parents=True)
    venv_nauro.write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": str(venv_nauro)}}})
    )
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert calls == [str(venv_nauro)]
    assert "MCP           active (wired in 1/1 repos)" in result.output


def test_status_dead_trusted_command_still_wins_over_untrusted_caveat(tmp_path, monkeypatch):
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _setup_project(tmp_path, monkeypatch, repos=[repo1, repo2])
    _wire_repo_mcp(repo1)
    (repo2 / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nauro": {"command": "./setup-helper.sh"}}})
    )
    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda cmd, **kwargs: False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "MCP           BROKEN" in result.output


def test_status_dedupes_shared_command_to_one_probe(tmp_path, monkeypatch):
    """N repos sharing one recorded command probe that command exactly once."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _setup_project(tmp_path, monkeypatch, repos=[repo1, repo2])
    _wire_repo_mcp(repo1)
    _wire_repo_mcp(repo2)
    calls: list[str] = []
    monkeypatch.setattr(
        nauro_command, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
    )

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert calls == ["nauro"]  # deduped to a single probe
    assert "MCP           active (wired in 2/2 repos)" in result.output


def test_status_no_project_shows_friendly_message(tmp_path, monkeypatch):
    """No resolvable project surfaces the status-specific guidance with exit 1.

    ``resolve_target_project`` raises ``typer.Exit``, which is not a
    ``SystemExit`` subclass — the friendly message only reaches the user when
    the handler catches the right exception type.
    """
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "No project found. Run 'nauro init <name>' to get started." in result.output


def _write_quarantine_backup(store, remote_path: str, content: bytes = b"remote body\n") -> None:
    from nauro.sync.quarantine import save_quarantine_backup

    save_quarantine_backup(store, remote_path, content, '"etag"')


def test_status_names_unresolved_quarantined_collisions(tmp_path, monkeypatch):
    """A quarantine records no sync state, so status is what keeps it visible."""
    store = _setup_project(tmp_path, monkeypatch)
    _write_quarantine_backup(store, "decisions/003-remote.md")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Quarantined decision-number collisions: 1" in result.output
    assert "decisions/003-remote.md" in result.output


def test_status_drops_a_quarantine_once_the_remote_file_is_tracked(tmp_path, monkeypatch):
    from nauro.sync.state import FileState, SyncState, save_state

    store = _setup_project(tmp_path, monkeypatch)
    _write_quarantine_backup(store, "decisions/003-remote.md")
    state = SyncState()
    state.files["decisions/003-remote.md"] = FileState(local_sha256="abc", remote_etag='"e"')
    save_state(store, state)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Quarantined decision-number collisions" not in result.output


def test_status_says_nothing_about_quarantines_when_there_are_none(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Quarantined" not in result.output


def test_status_says_quarantines_are_unreadable_rather_than_absent(tmp_path, monkeypatch):
    """A broken sync-state file must not silently turn a real quarantine into
    a clean report."""
    store = _setup_project(tmp_path, monkeypatch)
    _write_quarantine_backup(store, "decisions/003-remote.md")
    (store / ".sync-state.json").write_text("[]")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Quarantined decision-number collisions: could not be read" in result.output
