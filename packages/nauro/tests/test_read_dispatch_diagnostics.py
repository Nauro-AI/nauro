from __future__ import annotations

from unittest.mock import Mock

import pytest

from nauro.mcp import read_dispatch as dispatch
from nauro.mcp import stdio_server
from nauro.store.registry import register_project_v2, save_registry_v2
from nauro.store.repo_config import save_repo_config
from nauro.store.resolution import (
    DisconnectedProject,
    DisconnectedProjectError,
    NoProjectError,
    resolve_project_binding,
)

CASES = (
    [("get_context", {"level": level}) for level in ("L0", "L1", "L2")]
    + [("get_decision", {"number": 1, "mode": mode}) for mode in ("header", "full")]
    + [
        ("get_raw_file", {"path": "state.md"}),
        ("list_decisions", {}),
        ("search_decisions", {"query": "diagnostics"}),
        ("check_decision", {"proposed_approach": "Preserve diagnostics"}),
        ("diff_since_last_session", {}),
    ]
)
PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


def forbidden(*args, **kwargs):
    pytest.fail("A binding diagnostic must not access project content")


@pytest.mark.parametrize("name,kwargs", CASES)
@pytest.mark.parametrize("mode", [None, "local", "cloud"])
def test_binding_diagnostic_matches_live_result(tmp_path, monkeypatch, name, kwargs, mode):
    repo = tmp_path / "repo"
    repo.mkdir()
    if mode is not None:
        config = {"mode": mode, "id": PID, "name": "Diagnostic Project"}
        if mode == "cloud":
            config["server_url"] = "https://example.test"
        save_repo_config(repo, config)
    expected = getattr(stdio_server, name)(cwd=str(repo), **kwargs)
    monkeypatch.setattr(dispatch, "observe_generation_marker", forbidden)
    monkeypatch.setattr(dispatch, "read_active_user_id", forbidden)
    monkeypatch.setattr(dispatch.legacy, f"tool_{name}", forbidden)
    monkeypatch.setattr(dispatch.generation, name, forbidden)
    actual = getattr(dispatch, name)(cwd=str(repo), **kwargs)
    assert actual == expected
    assert actual.isError is False
    if mode is None:
        assert actual.structuredContent is None
    else:
        assert actual.structuredContent["project_id"] == PID
        assert actual.structuredContent["project_mode"] == mode
        assert actual.structuredContent["reason_code"] == "not_connected_on_this_machine"
        assert actual.structuredContent["recovery_actions"] == (
            ["locate", "restore", "continue"] if mode == "cloud" else ["locate", "continue"]
        )
        assert actual.content[0].text == actual.structuredContent["guidance"]


@pytest.mark.parametrize("defect", ["registry", "config", "unknown_project"])
def test_invalid_binding_does_not_become_onboarding(tmp_path, monkeypatch, defect):
    if defect == "registry":
        home = tmp_path / "home"
        home.mkdir()
        (home / "registry.json").write_text("PRIVATE CORRUPT REGISTRY")
    elif defect == "config":
        config = tmp_path / ".nauro"
        config.mkdir()
        (config / "config.json").write_text("PRIVATE CORRUPT CONFIG")
    monkeypatch.setattr(dispatch, "observe_generation_marker", forbidden)
    monkeypatch.setattr(dispatch.legacy, "tool_get_context", forbidden)
    result = dispatch.get_context(project_id=PID if defect == "unknown_project" else None)
    assert result.isError is True
    assert result.structuredContent is None
    assert result.content[0].text == "Error: Project read authority is unavailable."


@pytest.mark.parametrize("phase", ["marker", "generation"])
@pytest.mark.parametrize("kind", ["no_project", "disconnected"])
def test_post_binding_error_never_becomes_diagnostic(tmp_path, monkeypatch, phase, kind):
    pid, store = register_project_v2("Bound", [])
    store.mkdir(parents=True, exist_ok=True)
    binding = resolve_project_binding(pid, None)
    if kind == "no_project":
        error = NoProjectError("PRIVATE FAILURE")
    else:
        error = DisconnectedProjectError(
            DisconnectedProject(
                store_path=store,
                project_id=pid,
                display_name="PRIVATE LABEL",
                mode="cloud",
                reason_code="connected_record_missing",
                recovery_actions=("locate", "restore", "continue"),
                guidance="PRIVATE GUIDANCE",
            )
        )
    monkeypatch.setattr(dispatch, "resolve_project_binding", lambda *args: binding)
    monkeypatch.setattr(dispatch.legacy, "tool_get_context", forbidden)
    monkeypatch.setattr(dispatch, "_legacy_result", forbidden)
    if phase == "marker":
        monkeypatch.setattr(dispatch, "observe_generation_marker", Mock(side_effect=error))
    else:
        monkeypatch.setattr(dispatch, "observe_generation_marker", lambda *args: b"marker")
        monkeypatch.setattr(dispatch, "read_active_user_id", lambda: "actor")
        monkeypatch.setattr(dispatch.generation, "get_context", Mock(side_effect=error))
    result = dispatch.get_context(project_id=pid)
    assert result.isError is True
    assert result.structuredContent is None
    assert result.content[0].text == "Error: Project read authority is unavailable."


@pytest.mark.parametrize("name,kwargs", CASES)
@pytest.mark.parametrize("mode", ["local", "cloud"])
@pytest.mark.parametrize(
    "reason", ["connected_record_missing", "connected_record_invalid", "connected_binding_conflict"]
)
def test_existing_connection_diagnostics_match_live(
    tmp_path, monkeypatch, name, kwargs, mode, reason
):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = {"mode": mode, "id": PID, "name": "Diagnostic Project"}
    entry = {"mode": mode, "name": "Diagnostic Project", "repo_paths": [str(repo)]}
    if mode == "cloud":
        config["server_url"] = "https://example.test"
        entry["server_url"] = "https://example.test"
    if reason == "connected_record_invalid":
        external = tmp_path / "external" / PID
        external.mkdir(parents=True)
        entry["store_path"] = str(external)
    elif reason == "connected_binding_conflict":
        entry["name"] = "Conflicting Label"
    save_registry_v2({"schema_version": 2, "projects": {PID: entry}})
    save_repo_config(repo, config)
    expected = getattr(stdio_server, name)(cwd=str(repo), **kwargs)
    monkeypatch.setattr(dispatch, "observe_generation_marker", forbidden)
    monkeypatch.setattr(dispatch.legacy, f"tool_{name}", forbidden)
    actual = getattr(dispatch, name)(cwd=str(repo), **kwargs)
    assert actual == expected
    assert actual.isError is False
    assert actual.structuredContent["reason_code"] == reason
    assert actual.structuredContent["project_id"] == PID
    assert actual.structuredContent["recovery_actions"] == (
        ["locate", "restore", "continue"]
        if mode == "cloud" and reason == "connected_record_missing"
        else ["locate", "continue"]
    )
