import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.integrations import orchestrator, user_scope
from nauro.cli.main import app
from nauro.setup.outcomes import CodexConfigKind, CodexConfigOutcome
from nauro.store.home import registry_file
from nauro.store.local_files import UnreadableFileError


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, set()),
        (b'{"schema_version":2,"projects":{}}', set()),
        (b'{"schema_version":2,"projects":{"current":{}}}', {"current"}),
        (b'{"schema_version":2,"projects":{"other":{}}}', {"other"}),
        (b"{", None),
        (b"\xff", None),
        (b"[]", None),
        (b"null", None),
        (b'{"projects":{}}', None),
        (b'{"schema_version":3,"projects":{}}', None),
        (b'{"schema_version":2}', None),
        (b'{"schema_version":2,"projects":[]}', None),
        (b'{"schema_version":2,"projects":{"current":null}}', None),
        pytest.param(b"[" * 2000, None, id="excessive-nesting"),
    ],
)
def test_registry_evidence_distinguishes_unknown(raw, expected):
    if raw is not None:
        registry_file().write_bytes(raw)
    assert user_scope._registered_project_keys() == expected
    assert user_scope._user_scope_safe_to_clear("current") is (expected in (set(), {"current"}))


def test_permission_failure_is_unknown(monkeypatch):
    def unreadable(path):
        raise UnreadableFileError(path, "permission denied")

    monkeypatch.setattr(user_scope, "read_text_or_absent", unreadable)
    assert user_scope._registered_project_keys() is None
    assert user_scope._user_scope_safe_to_clear(None) is False


@pytest.mark.parametrize(
    "raw, preserved",
    [
        (b"{", True),
        (b'{"schema_version":2,"projects":{"p":{}}}', True),
        (b'{"schema_version":2,"projects":{}}', False),
    ],
)
def test_standalone_codex_obeys_evidence(raw, preserved):
    target = Path.home() / ".codex/config.toml"
    target.parent.mkdir(parents=True)
    original = (
        '[mcp_servers.nauro]\ncommand = "nauro"\nargs = ["serve", "--stdio"]\n\n'
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    target.write_text(original)
    registry_file().write_bytes(raw)
    result = CliRunner().invoke(app, ["setup", "codex", "--remove"])
    assert result.exit_code == 0, result.output
    if preserved:
        assert target.read_text() == original
        assert "preserved nauro entry" in result.output
        if raw == b"{":
            assert "registry evidence is unreadable" in result.output
            assert "projects registered" not in result.output
    else:
        assert target.read_text() == '[mcp_servers.other]\ncommand = "other"\n'


@pytest.mark.parametrize(
    "projects, override, preserved",
    [
        (None, None, True),
        (None, True, True),
        (None, False, True),
        ({"other": {}}, True, True),
        ({"current": {}}, False, True),
        ({}, False, True),
        ({"current": {}}, None, False),
        ({}, True, False),
    ],
)
def test_all_preserves_shared_artifacts_but_removes_local(tmp_path, projects, override, preserved):
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator.setup_all_surfaces(
        [repo], remove=False, with_skills=True, with_subagents=True, with_hooks=True
    )
    home = Path.home()
    shared = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert home / ".cursor/mcp.json" in shared
    assert home / ".codex/config.toml" in shared
    for folder in [".claude/skills", ".agents/skills", ".claude/agents", ".codex/agents"]:
        assert any((home / folder) in p.parents for p in shared)
    assert (repo / ".mcp.json").is_file()
    registry_file().write_text(
        "{" if projects is None else json.dumps({"schema_version": 2, "projects": projects})
    )
    outcomes = orchestrator.setup_all_surfaces(
        [repo],
        remove=True,
        current_project_key="current",
        with_skills=True,
        with_subagents=True,
        with_hooks=True,
        clear_user_scope_override=override,
    )
    codex = next(o for o in outcomes if isinstance(o, CodexConfigOutcome))
    assert codex.kind is (
        CodexConfigKind.PRESERVED_OTHER_PROJECTS if preserved else CodexConfigKind.REMOVED
    )
    if preserved:
        assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == shared
    else:
        assert not (home / ".cursor/mcp.json").exists()
        for folder in [".claude/skills", ".agents/skills", ".claude/agents", ".codex/agents"]:
            assert not any((home / folder).rglob("*.*"))
    assert not (repo / ".mcp.json").exists()
    for config in [repo / ".claude/settings.local.json", repo / ".codex/hooks.json"]:
        assert not config.exists() or "nauro" not in config.read_text()


def test_setup_all_does_not_bypass_invalid_project_resolution():
    target = Path.home() / ".codex/config.toml"
    target.parent.mkdir(parents=True)
    target.write_text('[mcp_servers.nauro]\ncommand = "nauro"\n')
    before = target.read_bytes()
    registry_file().write_text('{"schema_version":99,"projects":{}}')
    result = CliRunner().invoke(app, ["setup", "all", "--project", "example", "--remove"])
    assert result.exit_code == 1
    assert target.read_bytes() == before
