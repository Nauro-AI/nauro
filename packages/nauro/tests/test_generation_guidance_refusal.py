import json
import socket

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.payloads import build_l0_payload
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.agents_md import regenerate_agents_md_for_project


def tree(root):
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink()))
            if path.is_symlink()
            else ("dir", None)
            if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
    }


@pytest.fixture(params=["valid", "corrupt", "empty", "dangling"])
def replica(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.chdir(repo)
    pid, store = register_project_v2(
        "Replica", [repo], mode="cloud", server_url="https://probe.example"
    )
    save_repo_config(
        repo, {"mode": "cloud", "id": pid, "name": "Replica", "server_url": "https://probe.example"}
    )
    store.mkdir(parents=True, exist_ok=True)
    (store / "project.md").write_text("Preserved legacy evidence")
    (repo / "AGENTS.md").write_text("Preserved guidance")
    control = store / ".replica"
    if request.param == "dangling":
        control.symlink_to(store / "missing")
    else:
        control.mkdir()
        if request.param != "empty":
            marker = GenerationAuthorityMarker(
                schema_version=1, authority="generation", project_id=pid, store_format_version=1
            ).canonical_bytes()
            (control / "authority.json").write_bytes(
                marker if request.param == "valid" else b"broken"
            )

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy guidance or setup side effect ran")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("nauro.mcp.payloads.FilesystemStore", forbidden)
    monkeypatch.setattr("nauro.cli.commands.hook._check", forbidden)
    monkeypatch.setattr("nauro.cli.commands.hook._record_seen", forbidden)
    for name in (
        "_configure_mcp",
        "_configure_codex",
        "_remove_claude_md",
        "_all_claude_code_lines",
    ):
        monkeypatch.setattr(f"nauro.cli.integrations.orchestrator.{name}", forbidden)
    return tmp_path, repo, pid, store


@pytest.mark.parametrize("surface", ["payload", "regenerate"])
def test_derived_guidance_refuses_without_rendering(replica, surface):
    root, _, pid, store = replica
    before = tree(root)
    with pytest.raises(PermissionError, match="Legacy context generation is unavailable"):
        if surface == "payload":
            build_l0_payload(store)
        else:
            regenerate_agents_md_for_project(pid, store)
    assert tree(root) == before


@pytest.mark.parametrize("surface", ["claude-code", "all", "codex"])
def test_setup_refuses_before_wiring_or_guidance(replica, surface):
    root, _, _, _ = replica
    before = tree(root)
    result = CliRunner().invoke(app, ["setup", surface, "--with-hooks"])
    assert result.exit_code == 1
    assert "Legacy context generation is unavailable for generation replicas." in result.output
    assert tree(root) == before


@pytest.mark.parametrize("surface", ["user-prompt-submit", "codex-bootstrap"])
def test_hooks_remain_silent_without_legacy_context_or_state(replica, surface):
    root, repo, _, _ = replica
    before = tree(root)
    payload = {
        "cwd": str(repo),
        "prompt": "Review prior judgment",
        "session_id": "synthetic",
        "hook_event_name": "SessionStart",
    }
    result = CliRunner().invoke(app, ["hook", surface], input=json.dumps(payload))
    assert result.exit_code == 0
    assert result.output == ""
    assert tree(root) == before
