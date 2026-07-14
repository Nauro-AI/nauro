"""Tests for project-scoped Claude Code and Codex hook wiring.

Adds are idempotent, removes strip only Nauro entries, and wiring failures never
abort the rest of setup.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli._codex_hooks import _CODEX_HOOK_EVENTS, _CODEX_HOOK_SUBCOMMAND
from nauro.cli.commands.setup import (
    HOOK_EVENT_NAME,
    HOOK_SUBCOMMAND,
    HOOK_TIMEOUT_SECONDS,
    materialize_hooks_claude_code,
    materialize_hooks_codex,
    setup_all_surfaces,
)
from nauro.cli.main import app
from tests.conftest import register_v2_repo

runner = CliRunner()


def _settings(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def _codex_hooks(repo: Path) -> Path:
    return repo / ".codex" / "hooks.json"


def _nauro_entries(settings: dict) -> list[dict]:
    out = []
    for matcher in settings.get("hooks", {}).get(HOOK_EVENT_NAME, []):
        for entry in matcher.get("hooks", []):
            if isinstance(entry, dict) and HOOK_SUBCOMMAND in entry.get("command", ""):
                out.append(entry)
    return out


def _codex_nauro_entries(config: dict, event: str) -> list[dict]:
    out = []
    for matcher in config.get("hooks", {}).get(event, []):
        for entry in matcher.get("hooks", []):
            fields = (entry.get("command", ""), entry.get("commandWindows", ""))
            if any(_CODEX_HOOK_SUBCOMMAND in value for value in fields):
                out.append(entry)
    return out


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    result = register_v2_repo(tmp_path, "hookproj", save_config=False, chdir=False)
    return result.repo, result.store_path


# ── direct helper: add path ────────────────────────────────────────────────────


def test_materialize_writes_correct_structure(tmp_path: Path):
    """The add path writes the canonical UserPromptSubmit hook entry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    line = materialize_hooks_claude_code(repo, remove=False)
    assert "wrote nauro hook" in line

    settings = json.loads(_settings(repo).read_text())
    entries = _nauro_entries(settings)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "command"
    # Command is the resolved nauro path + the hook subcommand, so it fires even
    # when nauro is off the agent's launch PATH. It still carries the "nauro hook"
    # marker the remove path matches on.
    assert entry["command"].endswith(HOOK_SUBCOMMAND)
    assert "nauro hook" in entry["command"]
    assert entry["timeout"] == HOOK_TIMEOUT_SECONDS
    # The MVP install is BM25-only: it must not set the embeddings flag.
    assert "NAURO_EMBEDDINGS" not in entry["command"]


def test_materialize_is_idempotent(tmp_path: Path):
    """A re-run does not duplicate the nauro hook entry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize_hooks_claude_code(repo, remove=False)
    line = materialize_hooks_claude_code(repo, remove=False)
    assert "already present" in line

    settings = json.loads(_settings(repo).read_text())
    assert len(_nauro_entries(settings)) == 1


# ── direct helper: remove path ─────────────────────────────────────────────────


def test_remove_strips_only_nauro_entry(tmp_path: Path):
    """Remove deletes the nauro hook but preserves user-authored hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings_path = _settings(repo)
    settings_path.parent.mkdir(parents=True)
    # A user hook on the same event, plus other settings, must survive.
    user_settings = {
        "hooks": {
            HOOK_EVENT_NAME: [
                {"hooks": [{"type": "command", "command": "my-own-linter --check"}]},
            ]
        },
        "model": "claude-opus",
    }
    settings_path.write_text(json.dumps(user_settings))

    materialize_hooks_claude_code(repo, remove=False)
    # Both the user hook and the nauro hook are now present.
    after_add = json.loads(settings_path.read_text())
    assert len(_nauro_entries(after_add)) == 1

    line = materialize_hooks_claude_code(repo, remove=True)
    assert "removed nauro hook" in line

    after_remove = json.loads(settings_path.read_text())
    assert _nauro_entries(after_remove) == []
    # User hook and unrelated settings preserved.
    commands = [e["command"] for m in after_remove["hooks"][HOOK_EVENT_NAME] for e in m["hooks"]]
    assert "my-own-linter --check" in commands
    assert after_remove["model"] == "claude-opus"


def test_remove_when_absent_is_noop(tmp_path: Path):
    """Removing with no nauro hook present reports nothing to remove."""
    repo = tmp_path / "repo"
    repo.mkdir()
    line = materialize_hooks_claude_code(repo, remove=True)
    assert "no nauro hook to remove" in line


def test_remove_deletes_empty_settings_file(tmp_path: Path):
    """When the nauro hook was the only content, the file is unlinked on remove."""
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize_hooks_claude_code(repo, remove=False)
    assert _settings(repo).is_file()
    materialize_hooks_claude_code(repo, remove=True)
    assert not _settings(repo).is_file()


def test_claude_hook_round_trip_preserves_empty_user_matcher(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings_path = _settings(repo)
    settings_path.parent.mkdir(parents=True)
    user_matcher = {"matcher": "startup", "hooks": []}
    settings_path.write_text(
        json.dumps({"hooks": {HOOK_EVENT_NAME: [user_matcher]}}),
        encoding="utf-8",
    )

    materialize_hooks_claude_code(repo, remove=False)
    materialize_hooks_claude_code(repo, remove=True)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {"hooks": {HOOK_EVENT_NAME: [user_matcher]}}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_claude_hook_add_and_remove_refuse_symlinked_settings(tmp_path: Path):
    """Both hook paths refuse a symlinked .claude/settings.json untouched."""
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    outside = tmp_path / "outside-settings.json"
    outside.write_text("{}")
    (repo / ".claude" / "settings.json").symlink_to(outside)

    add_line = materialize_hooks_claude_code(repo, remove=False)
    remove_line = materialize_hooks_claude_code(repo, remove=True)

    assert "refused to modify" in add_line
    assert "refused to modify" in remove_line
    assert outside.read_text() == "{}"
    assert (repo / ".claude" / "settings.json").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_codex_hooks_refuse_symlinked_hooks_json(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".codex").mkdir(parents=True)
    outside = tmp_path / "outside-hooks.json"
    outside.write_text("{}")
    (repo / ".codex" / "hooks.json").symlink_to(outside)

    add_line = materialize_hooks_codex(repo, remove=False)
    remove_line = materialize_hooks_codex(repo, remove=True)

    assert "refused to modify" in add_line
    assert "refused to modify" in remove_line
    assert outside.read_text() == "{}"
    assert (repo / ".codex" / "hooks.json").is_symlink()


def test_claude_hook_remove_preserves_matcher_metadata(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize_hooks_claude_code(repo, remove=False)
    settings_path = _settings(repo)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    nauro_entry = _nauro_entries(settings)[0]
    settings["hooks"][HOOK_EVENT_NAME] = [
        {"matcher": "startup", "custom": "keep", "hooks": [nauro_entry]}
    ]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    materialize_hooks_claude_code(repo, remove=True)

    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert after["hooks"][HOOK_EVENT_NAME] == [
        {"matcher": "startup", "custom": "keep", "hooks": []}
    ]


def test_materialize_codex_writes_both_lifecycle_events(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    command = "/opt/Nauro Tool/bin/nauro"

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: command)
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()
    line = materialize_hooks_codex(repo, remove=False)

    assert "wrote nauro hooks" in line
    config = json.loads(_codex_hooks(repo).read_text())
    for event in _CODEX_HOOK_EVENTS:
        entries = _codex_nauro_entries(config, event)
        assert len(entries) == 1
        assert entries[0]["command"] == (
            "test -x '/opt/Nauro Tool/bin/nauro' || exit 0; "
            "exec '/opt/Nauro Tool/bin/nauro' hook codex-bootstrap"
        )
        assert entries[0]["commandWindows"] == (
            "powershell.exe -NoLogo -NoProfile -NonInteractive -Command "
            "\"if (Test-Path -LiteralPath '/opt/Nauro Tool/bin/nauro' -PathType Leaf) "
            "{ & '/opt/Nauro Tool/bin/nauro' hook codex-bootstrap }; exit 0\""
        )
        assert entries[0]["timeout"] == 10
        assert entries[0]["statusMessage"] == "Loading Nauro project context"


def test_materialize_codex_warns_for_untracked_hooks_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    line = materialize_hooks_codex(repo, remove=False)

    assert ".codex/hooks.json is untracked and not git-ignored" in line
    assert "local Nauro wiring" in line


def test_materialize_codex_uses_current_install_when_durable_command_is_too_old(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: "/opt/old/nauro")
    monkeypatch.setattr(
        setup_mod, "_interpreter_sibling_candidate", lambda: "/repo/.venv/bin/nauro"
    )
    monkeypatch.setattr(
        setup_mod.cli_utils,
        "probe_nauro_command",
        lambda command, **kwargs: command == "/repo/.venv/bin/nauro",
    )
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()

    line = materialize_hooks_codex(repo, remove=False)

    assert "wrote nauro hooks" in line
    config = json.loads(_codex_hooks(repo).read_text())
    entry = _codex_nauro_entries(config, "SessionStart")[0]
    assert "/repo/.venv/bin/nauro" in entry["command"]
    assert "/opt/old/nauro" not in entry["command"]
    assert "does not support Codex bootstrap hooks" in capsys.readouterr().err


def test_materialize_codex_skips_when_no_compatible_command(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: "/opt/old/nauro")
    monkeypatch.setattr(setup_mod, "_interpreter_sibling_candidate", lambda: None)
    monkeypatch.setattr(setup_mod.cli_utils, "probe_nauro_command", lambda command, **kwargs: False)
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()

    line = materialize_hooks_codex(repo, remove=False)

    assert line == f"  {repo}: Codex hook wiring skipped; no compatible Nauro command"
    assert not _codex_hooks(repo).exists()


def test_materialize_codex_validates_config_before_resolving_command(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    hooks_path = _codex_hooks(repo)
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text('{"hooks": []}', encoding="utf-8")

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_interpreter_sibling_candidate", lambda: "/opt/nauro")
    monkeypatch.setattr(
        setup_mod.cli_utils,
        "probe_nauro_command",
        lambda *args, **kwargs: pytest.fail("command resolution should not run"),
    )

    line = materialize_hooks_codex(repo, remove=False)

    assert line == (f"  {repo}: hooks key in .codex/hooks.json is not a JSON object, skipped")


@pytest.mark.skipif(os.name == "nt", reason="POSIX command guard")
def test_codex_hook_missing_binary_guard_exits_zero(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = tmp_path / "missing nauro"

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: str(missing))
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()
    materialize_hooks_codex(repo, remove=False)
    config = json.loads(_codex_hooks(repo).read_text())
    entry = _codex_nauro_entries(config, "SessionStart")[0]

    result = subprocess.run(["sh", "-c", entry["command"]], capture_output=True, text=True)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX command guard")
def test_codex_hook_bare_command_guard_checks_path(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: "nauro")
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()
    materialize_hooks_codex(repo, remove=False)
    config = json.loads(_codex_hooks(repo).read_text())
    entry = _codex_nauro_entries(config, "SessionStart")[0]

    result = subprocess.run(
        ["/bin/sh", "-c", entry["command"]],
        capture_output=True,
        text=True,
        env={"PATH": ""},
    )

    assert "command -v nauro" in entry["command"]
    assert entry["commandWindows"] == (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -Command "
        "\"if (Get-Command 'nauro' -ErrorAction SilentlyContinue) "
        "{ & 'nauro' hook codex-bootstrap }; exit 0\""
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_materialize_codex_is_idempotent_and_preserves_user_hooks(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    hooks_path = _codex_hooks(repo)
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "load-notes"}],
                        }
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "cleanup"}]}],
                },
                "theme": "dark",
            }
        )
    )

    materialize_hooks_codex(repo, remove=False)
    writes = 0
    write_text = Path.write_text

    def count_hook_writes(path: Path, *args, **kwargs):
        nonlocal writes
        if path == hooks_path:
            writes += 1
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", count_hook_writes)
    line = materialize_hooks_codex(repo, remove=False)

    assert "already present" in line
    assert writes == 0
    config = json.loads(hooks_path.read_text())
    for event in _CODEX_HOOK_EVENTS:
        assert len(_codex_nauro_entries(config, event)) == 1
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "load-notes"
    assert config["hooks"]["Stop"][0]["hooks"][0]["command"] == "cleanup"
    assert config["theme"] == "dark"


def test_materialize_codex_preserves_empty_non_ascii_user_matcher(tmp_path: Path):
    repo = tmp_path / "repo"
    hooks_path = _codex_hooks(repo)
    hooks_path.parent.mkdir(parents=True)
    user_matcher = {"matcher": "démarrage", "hooks": []}
    hooks_path.write_text(
        json.dumps({"hooks": {"SessionStart": [user_matcher]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    materialize_hooks_codex(repo, remove=False)
    after_add = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert after_add["hooks"]["SessionStart"][0] == user_matcher

    materialize_hooks_codex(repo, remove=True)
    after_remove = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert after_remove == {"hooks": {"SessionStart": [user_matcher]}}


def test_materialize_codex_refreshes_recorded_command(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    import nauro.cli.commands.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: "/old/nauro")
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()
    materialize_hooks_codex(repo, remove=False)
    monkeypatch.setattr(setup_mod, "_resolve_nauro_command", lambda: "/new/nauro")
    setup_mod._find_nauro_command.cache_clear()
    setup_mod._find_nauro_codex_hook_command.cache_clear()

    line = materialize_hooks_codex(repo, remove=False)

    assert "wrote nauro hooks" in line
    config = json.loads(_codex_hooks(repo).read_text())
    for event in _CODEX_HOOK_EVENTS:
        entries = _codex_nauro_entries(config, event)
        assert len(entries) == 1
        assert "/new/nauro" in entries[0]["command"]
        assert "/old/nauro" not in entries[0]["command"]


def test_remove_codex_strips_only_nauro_entries(tmp_path: Path):
    repo = tmp_path / "repo"
    hooks_path = _codex_hooks(repo)
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "load-notes"}]}]
                },
                "theme": "dark",
            }
        )
    )
    materialize_hooks_codex(repo, remove=False)

    line = materialize_hooks_codex(repo, remove=True)

    assert "removed nauro hooks" in line
    config = json.loads(hooks_path.read_text())
    assert config["hooks"] == {
        "SessionStart": [{"hooks": [{"type": "command", "command": "load-notes"}]}]
    }
    assert config["theme"] == "dark"


def test_remove_codex_preserves_matcher_metadata_when_nauro_is_only_hook(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    materialize_hooks_codex(repo, remove=False)
    hooks_path = _codex_hooks(repo)
    config = json.loads(hooks_path.read_text(encoding="utf-8"))
    nauro_entry = _codex_nauro_entries(config, "SessionStart")[0]
    config["hooks"]["SessionStart"] = [
        {"matcher": "startup", "custom": "keep", "hooks": [nauro_entry]}
    ]
    hooks_path.write_text(json.dumps(config), encoding="utf-8")

    materialize_hooks_codex(repo, remove=True)

    after = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"] == [{"matcher": "startup", "custom": "keep", "hooks": []}]


# ── CLI integration via `setup claude-code --with-hooks` ───────────────────────


def test_setup_claude_code_with_hooks(tmp_path: Path, monkeypatch):
    """`setup claude-code --with-hooks` wires the hook for the project's repo."""
    repo, _store = _make_project(tmp_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code", "--with-hooks"])
    assert result.exit_code == 0, result.output

    settings = json.loads(_settings(repo).read_text())
    assert len(_nauro_entries(settings)) == 1


def test_setup_claude_code_without_hooks_writes_no_settings(tmp_path: Path, monkeypatch):
    """Without --with-hooks, no .claude/settings.json hook is written."""
    repo, _store = _make_project(tmp_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "claude-code"])
    assert result.exit_code == 0, result.output
    assert not _settings(repo).is_file()


def test_setup_claude_code_remove_cleans_hooks_without_with_hooks(tmp_path: Path, monkeypatch):
    """`setup claude-code --remove` strips the nauro hook even without
    --with-hooks, matching `setup codex --remove` and `setup all --remove`,
    while a plugin-authored hook on the same event survives untouched."""
    repo, _store = _make_project(tmp_path)
    monkeypatch.chdir(repo)

    added = runner.invoke(app, ["setup", "claude-code", "--with-hooks"])
    assert added.exit_code == 0, added.output

    settings_path = _settings(repo)
    settings = json.loads(settings_path.read_text())
    plugin_entry = {
        "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/prompt-hook-nauro.sh",
    }
    settings["hooks"][HOOK_EVENT_NAME].append({"hooks": [plugin_entry]})
    settings_path.write_text(json.dumps(settings))

    result = runner.invoke(app, ["setup", "claude-code", "--remove"])

    assert result.exit_code == 0, result.output
    assert "removed nauro hook" in result.output
    after = json.loads(settings_path.read_text())
    assert _nauro_entries(after) == []
    remaining = [e for m in after["hooks"][HOOK_EVENT_NAME] for e in m["hooks"]]
    assert remaining == [plugin_entry]


def test_setup_codex_with_hooks_wires_project_repos_and_prints_trust_guidance(
    tmp_path: Path, monkeypatch
):
    repo, _store = _make_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "codex", "--with-hooks"])

    assert result.exit_code == 0, result.output
    config = json.loads(_codex_hooks(repo).read_text())
    for event in _CODEX_HOOK_EVENTS:
        assert len(_codex_nauro_entries(config, event)) == 1
    assert "/hooks" in result.output
    assert "review and trust" in result.output


def test_setup_codex_remove_with_hooks_preserves_user_entries(tmp_path: Path, monkeypatch):
    repo, _store = _make_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(repo)
    runner.invoke(app, ["setup", "codex", "--with-hooks"])
    hooks_path = _codex_hooks(repo)
    config = json.loads(hooks_path.read_text())
    config["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "cleanup"}]}]
    hooks_path.write_text(json.dumps(config))

    result = runner.invoke(app, ["setup", "codex", "--remove", "--with-hooks"])

    assert result.exit_code == 0, result.output
    config = json.loads(hooks_path.read_text())
    assert config["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "cleanup"}]}]}


def test_setup_codex_remove_cleans_hooks_without_with_hooks(tmp_path: Path, monkeypatch):
    repo, _store = _make_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(repo)
    added = runner.invoke(app, ["setup", "codex", "--with-hooks"])
    assert added.exit_code == 0, added.output

    result = runner.invoke(app, ["setup", "codex", "--remove"])

    assert result.exit_code == 0, result.output
    assert not _codex_hooks(repo).exists()
    assert "removed nauro hooks" in result.output


def test_setup_codex_remove_outside_project_still_removes_global_config(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.nauro]\ncommand = "nauro"\nargs = ["serve", "--stdio"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["setup", "codex", "--remove", "--with-hooks"])

    assert result.exit_code == 0, result.output
    assert "Codex: removed nauro" in result.output
    assert "Project-scoped Codex hooks were not removed" in result.output
    assert "nauro" not in config_path.read_text(encoding="utf-8")


def test_setup_codex_remove_cleans_orphaned_repo_hooks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "orphaned-repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    materialize_hooks_codex(repo, remove=False)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["setup", "codex", "--remove"])

    assert result.exit_code == 0, result.output
    assert "removed nauro hooks" in result.output
    assert not _codex_hooks(repo).exists()


# ── setup all integration ──────────────────────────────────────────────────────


def test_setup_all_with_hooks_wires_claude_code_and_codex(tmp_path: Path):
    repo, _store = _make_project(tmp_path)
    lines = setup_all_surfaces([repo], with_hooks=True)
    assert any("nauro hook" in line for line in lines)

    settings = json.loads(_settings(repo).read_text())
    assert len(_nauro_entries(settings)) == 1
    codex_config = json.loads(_codex_hooks(repo).read_text())
    for event in _CODEX_HOOK_EVENTS:
        assert len(_codex_nauro_entries(codex_config, event)) == 1
    assert not (repo / ".cursor" / "settings.json").exists()


def test_setup_all_with_hooks_prints_codex_trust_guidance(tmp_path: Path, monkeypatch):
    repo, _store = _make_project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["setup", "all", "--with-hooks"])

    assert result.exit_code == 0, result.output
    assert "/hooks" in result.output
    assert "review and trust" in result.output


def test_setup_all_hook_failure_does_not_abort(tmp_path: Path, monkeypatch):
    """A hook-wiring failure is caught and reported, not propagated."""
    repo, _store = _make_project(tmp_path)

    import nauro.cli.commands.setup as setup_mod

    def boom(repo, *, remove):
        raise RuntimeError("simulated wiring failure")

    monkeypatch.setattr(setup_mod, "materialize_hooks_claude_code", boom)

    # Must not raise; the rest of setup still produces its lines.
    lines = setup_all_surfaces([repo], with_hooks=True)
    assert any("hook" in line and "error" in line for line in lines)
    # MCP wiring still happened despite the hook failure.
    assert (repo / ".mcp.json").is_file()


def test_setup_all_without_hooks_writes_nothing(tmp_path: Path):
    """Default setup_all_surfaces does not touch .claude/settings.json."""
    repo, _store = _make_project(tmp_path)
    setup_all_surfaces([repo])
    assert not _settings(repo).is_file()
    assert not _codex_hooks(repo).is_file()


def test_setup_all_remove_cleans_existing_hooks_without_with_hooks(tmp_path: Path):
    repo, _store = _make_project(tmp_path)
    setup_all_surfaces([repo], with_hooks=True)

    setup_all_surfaces([repo], remove=True)

    assert not _settings(repo).exists()
    assert not _codex_hooks(repo).exists()


def test_is_nauro_hook_matches_regardless_of_entrypoint_name():
    """The remove/idempotency marker must recognise the nauro hook however the
    entrypoint resolved — bare name, absolute POSIX path, or Windows .exe —
    otherwise --remove orphans the entry and re-running setup duplicates it."""
    from nauro.cli.commands.setup import _is_nauro_hook

    for cmd in (
        "nauro hook user-prompt-submit",
        "/opt/venv/bin/nauro hook user-prompt-submit",
        r"C:\Users\me\Scripts\nauro.exe hook user-prompt-submit",
    ):
        assert _is_nauro_hook({"type": "command", "command": cmd}), cmd
    # A user's own UserPromptSubmit hook is left alone.
    assert not _is_nauro_hook({"type": "command", "command": "my-linter --check"})
