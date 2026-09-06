from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from nauro.mcp import stdio_server
from nauro.store import read_authority
from nauro.store.registry import register_project_v2


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    pid, store = register_project_v2(
        "Startup", [repo], mode="cloud", server_url="https://example.test"
    )
    store.mkdir(parents=True, exist_ok=True)
    pull = Mock(return_value=0)
    monkeypatch.setattr("nauro.sync.hooks.pull_before_session", pull)
    return pid, store, pull


def marker_bytes(pid: str, version: int = 1) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "authority": "generation",
            "project_id": pid,
            "store_format_version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize("empty_control", [False, True])
def test_cloud_legacy_reaches_existing_pull(startup, empty_control):
    pid, store, pull = startup
    if empty_control:
        (store / ".replica").mkdir()
    stdio_server._pull_on_startup()
    pull.assert_called_once_with(pid, store)


@pytest.mark.parametrize("case", ["valid", "corrupt", "wrong_project", "unsupported", "directory"])
def test_marker_prevents_startup_pull(startup, case):
    pid, store, pull = startup
    control = store / ".replica"
    control.mkdir()
    marker = control / "authority.json"
    if case == "directory":
        marker.mkdir()
    else:
        payload = marker_bytes(pid)
        if case == "corrupt":
            payload = b"PRIVATE CORRUPT CONTROL"
        elif case == "wrong_project":
            payload = marker_bytes("01K33333333333333333333333")
        elif case == "unsupported":
            payload = marker_bytes(pid, 2)
        marker.write_bytes(payload)
    stdio_server._pull_on_startup()
    pull.assert_not_called()


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unreadable_control_prevents_pull_without_logging_payload(
    startup, monkeypatch, caplog, error
):
    _, _, pull = startup

    def unavailable(path: Path) -> bytes | None:
        raise error("PRIVATE CONTROL DETAIL")

    monkeypatch.setattr(read_authority, "_read_optional_file", unavailable)
    stdio_server._pull_on_startup()
    pull.assert_not_called()
    assert "session-start pull: unavailable, continuing with local state" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_missing_store_prevents_pull(startup):
    _, store, pull = startup
    store.rmdir()
    stdio_server._pull_on_startup()
    pull.assert_not_called()


@pytest.mark.parametrize("location", ["marker", "parent"])
def test_linked_control_prevents_pull(startup, tmp_path, location):
    pid, store, pull = startup
    target = tmp_path / "target"
    target.mkdir()
    (target / "authority.json").write_bytes(marker_bytes(pid))
    control = store / ".replica"
    try:
        if location == "parent":
            control.symlink_to(target, target_is_directory=True)
        else:
            control.mkdir()
            (control / "authority.json").symlink_to(target / "authority.json")
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")
    stdio_server._pull_on_startup()
    pull.assert_not_called()


def test_invalid_registry_prevents_pull(startup, tmp_path):
    _, _, pull = startup
    registry = tmp_path / "home" / "registry.json"
    assert registry.is_file()
    registry.write_bytes(b"PRIVATE INVALID REGISTRY")
    stdio_server._pull_on_startup()
    pull.assert_not_called()


def test_hardlinked_marker_prevents_pull(startup, tmp_path):
    pid, store, pull = startup
    target = tmp_path / "marker"
    target.write_bytes(marker_bytes(pid))
    control = store / ".replica"
    control.mkdir()
    (control / "authority.json").hardlink_to(target)
    stdio_server._pull_on_startup()
    pull.assert_not_called()
