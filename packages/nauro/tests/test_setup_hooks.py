"""Tests for ``--with-hooks`` — wiring the advisory hook into .claude/settings.json.

The hook is wired into project-scope ``<repo>/.claude/settings.json`` under
``hooks.UserPromptSubmit``. Adds are idempotent; removes strip only the
nauro-authored entry and preserve user hooks. The wiring is Claude-Code-only and
never aborts the rest of setup.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.commands.setup import (
    HOOK_EVENT_NAME,
    HOOK_SUBCOMMAND,
    HOOK_TIMEOUT_SECONDS,
    materialize_hooks_claude_code,
    setup_all_surfaces,
)
from nauro.cli.main import app
from tests.conftest import register_v2_repo

runner = CliRunner()


def _settings(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def _nauro_entries(settings: dict) -> list[dict]:
    out = []
    for matcher in settings.get("hooks", {}).get(HOOK_EVENT_NAME, []):
        for entry in matcher.get("hooks", []):
            if isinstance(entry, dict) and HOOK_SUBCOMMAND in entry.get("command", ""):
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


# ── setup all integration ──────────────────────────────────────────────────────


def test_setup_all_with_hooks_claude_code_only(tmp_path: Path):
    """setup_all_surfaces with hooks wires Claude Code only — no Cursor/Codex hook."""
    repo, _store = _make_project(tmp_path)
    lines = setup_all_surfaces([repo], with_hooks=True)
    assert any("nauro hook" in line for line in lines)

    settings = json.loads(_settings(repo).read_text())
    assert len(_nauro_entries(settings)) == 1
    # No hook file is written under Cursor's surface.
    assert not (repo / ".cursor" / "settings.json").exists()


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
