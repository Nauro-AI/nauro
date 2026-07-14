"""Tests for nauro status command."""

import json

from typer.testing import CliRunner

import nauro.cli.commands.status as status_mod
from nauro.cli import utils as cli_utils
from nauro.cli.commands.setup import CODEX_HOOK_PROBE_ARGS
from nauro.cli.main import app
from nauro.store.registry import register_project
from nauro.templates.agents_md import FOOTER_MARKER
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path, monkeypatch, repos=None):
    """Register a project with the given repos (default: tmp_path itself).

    Also points the Codex-global probe at a path under tmp_path so a wired
    ~/.codex/config.toml on the developer's machine cannot leak into the
    detection assertions.
    """
    repos = repos if repos is not None else [tmp_path]
    store = register_project("testproj", repos)
    scaffold_project_store("testproj", store)
    monkeypatch.chdir(repos[0])
    monkeypatch.setattr(
        status_mod, "_codex_config_path", lambda: tmp_path / "codex-home" / "config.toml"
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


def test_status_mcp_and_agents_inactive_when_nothing_wired(tmp_path, monkeypatch):
    """No MCP config anywhere and no generated AGENTS.md → both rows inactive."""
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           inactive — run 'nauro setup all'" in result.output
    assert "Codex hooks   inactive - run 'nauro setup codex --with-hooks'" in result.output
    assert "AGENTS.md     inactive — run 'nauro sync'" in result.output
    assert "Decisions:" in result.output


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
    assert "AGENTS.md     inactive — run 'nauro sync'" in result.output


def test_status_corrupt_mcp_config_soft_fails(tmp_path, monkeypatch):
    """Unparseable wiring configs count as unwired; status never crashes."""
    _setup_project(tmp_path, monkeypatch)
    (tmp_path / ".mcp.json").write_text("{not valid json")
    codex_config = tmp_path / "codex-home" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text("mcp_servers = not-toml [")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MCP           inactive — run 'nauro setup all'" in result.output


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
    monkeypatch.setattr(cli_utils, "probe_nauro_command", lambda cmd, **kwargs: False)

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
        cli_utils, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
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
        cli_utils, "probe_nauro_command", lambda command, **kwargs: calls.append(command) or True
    )

    result = runner.invoke(app, ["status", "--no-probe"])

    assert result.exit_code == 0
    assert calls == []
    assert "Codex hooks   configured (wired in 1/1 repos; liveness not probed)" in result.output


def test_status_codex_hooks_broken_when_command_is_dead(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path, command="/gone/nauro")
    monkeypatch.setattr(cli_utils, "probe_nauro_command", lambda command, **kwargs: False)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   BROKEN" in result.output
    assert "recorded command won't run" in result.output


def test_status_codex_hooks_does_not_claim_health_when_probe_fails(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path)

    def fail_probe(_commands, _hook_commands):
        raise OSError("probe unavailable")

    monkeypatch.setattr(status_mod, "_probe_distinct_commands", fail_probe)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Codex hooks   configured (wired in 1/1 repos; liveness unknown)" in result.output
    assert "command healthy" not in result.output


def test_status_codex_hooks_broken_when_event_is_missing(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(tmp_path, events=("SessionStart",))
    calls: list[str] = []
    monkeypatch.setattr(
        cli_utils, "probe_nauro_command", lambda command, **kwargs: calls.append(command) or True
    )

    result = runner.invoke(app, ["status", "--no-probe"])

    assert result.exit_code == 0
    assert calls == []
    assert "Codex hooks   BROKEN" in result.output
    assert "lifecycle wiring is incomplete" in result.output


def test_status_probes_shared_mcp_and_hook_command_once(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(tmp_path)
    _wire_codex_hooks(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        cli_utils,
        "probe_nauro_command",
        lambda command, **kwargs: calls.append((command, kwargs["args"])) or True,
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert calls == [("nauro", CODEX_HOOK_PROBE_ARGS)]
    assert "MCP           active" in result.output
    assert "Codex hooks   configured" in result.output


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
        cli_utils, "probe_nauro_command", lambda cmd, **kwargs: calls.append(cmd) or True
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
