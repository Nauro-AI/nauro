from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import stdio_server
from nauro.store import read_authority
from nauro.store.generation_refresh_io import refresh_paths
from nauro.store.registry import register_project_v2
from nauro.store.resolution import resolve_project_binding
from nauro.sync import generation_refresh_status as refresh_status
from tests import test_generation_installation as fixtures
from tests.test_generation_attachment import _run
from tests.test_generation_attachment import hosted as attachment_fixture


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


def test_valid_marker_selects_refresh_before_legacy_pull(startup, monkeypatch):
    _, store, pull = startup
    (store / ".replica").mkdir()
    (store / ".replica/authority.json").write_bytes(marker_bytes(startup[0]))
    refresh = Mock()
    monkeypatch.setattr("nauro.sync.generation_refresh_status.refresh_replica", refresh)
    stdio_server._pull_on_startup()
    refresh.assert_called_once()
    assert refresh.call_args.args[0].store_path == store
    pull.assert_not_called()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    repo, connection, credentials, control, calls = attachment_fixture.__wrapped__(
        tmp_path, monkeypatch
    )
    assert _run(repo).exit_code == 0
    binding = resolve_project_binding(fixtures.PROJECT_ID, None, use_cwd=False)
    legacy = []
    monkeypatch.setattr("nauro.sync.hooks.pull_before_session", lambda *a: legacy.append(a))
    return repo, binding, connection, credentials, control, calls, legacy


def _status(binding):
    result = CliRunner().invoke(
        app, ["status", "--project", binding.project_id, "--json", "--no-probe"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["replica_status"]


def test_startup_status_and_rendered_read_bytes(installed):
    _, binding, _, _, _, _, legacy = installed
    before = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    stdio_server._pull_on_startup()
    after = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert before.content == after.content
    assert after.isError is False
    status = after.structuredContent["replica_status"]
    assert status["last_refresh_error_code"] is None
    assert status["last_refresh_succeeded_at"] is not None
    assert _status(binding) == status
    assert status["authorization_checked"] is False
    assert status["pending_outbox_count"] is None
    assert legacy == []


def test_remote_commit_startup_refresh_then_read(installed, monkeypatch):
    _, binding, _, _, _, _, legacy = installed
    monkeypatch.setattr(fixtures, "GENERATION_ID", "01K77777777777777777777777")
    new = fixtures._projection({"state.md": b"New committed state\n"})
    client_type = httpx.Client

    def wire(request):
        if request.url.path == "/generations/projection":
            return httpx.Response(
                200,
                json={
                    "projection": new.target.identity.model_dump(),
                    "manifest_base64": base64.b64encode(new.manifest_json).decode(),
                },
            )
        if request.url.path == "/generations/presign":
            return httpx.Response(
                200,
                json={
                    "projection": new.target.identity.model_dump(),
                    "urls": [{"path": "state.md", "url": "https://objects.example/state"}],
                    "expires_at": "2999-12-31T23:59:59Z",
                },
            )
        assert request.url.host == "objects.example"
        return httpx.Response(200, content=b"New committed state\n")

    # The attachment fixture replaced Client. Use its returned client's concrete type.
    with client_type(trust_env=False) as old:
        concrete = type(old)
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: concrete(transport=httpx.MockTransport(wire), **kw)
    )
    refused = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert refused.isError is True
    stdio_server._pull_on_startup()
    read = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert read.isError is False
    assert "New committed state" in read.content[0].text
    assert read.structuredContent["replica_status"]["generation_id"] == fixtures.GENERATION_ID
    assert legacy == []


@pytest.mark.parametrize("failure", ["expired", "revoked", "network"])
def test_startup_failure_is_visible_without_fallback(installed, monkeypatch, caplog, failure):
    _, binding, _, credentials, control, calls, legacy = installed
    stdio_server._pull_on_startup()
    previous = _status(binding)
    pointer = refresh_paths(binding, fixtures.USER_ID).pointer
    before = pointer.read_bytes()
    if failure == "expired":
        with credentials.locked():
            credentials.write(
                credentials.read().model_copy(update={"expires_at": int(time.time()) - 1})
            )
        calls.clear()
    elif failure == "revoked":
        control["status"] = 403
    else:
        monkeypatch.setattr(
            refresh_status,
            "recover_generation_refresh",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("PRIVATE")),
        )
    stdio_server._pull_on_startup()
    status = _status(binding)
    assert status["last_refresh_error_code"] in {
        "refresh_failed",
        "generation_connection_unavailable",
    }
    assert status["last_refresh_succeeded_at"] == previous["last_refresh_succeeded_at"]
    assert pointer.read_bytes() == before
    assert "session-start refresh: incomplete" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert legacy == []
    if failure == "expired":
        assert calls == []


def test_interrupted_attempt_remains_visible(installed, monkeypatch):
    _, binding, _, _, _, _, _ = installed
    with monkeypatch.context() as fault:
        fault.setattr(
            refresh_status,
            "recover_generation_refresh",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            stdio_server._pull_on_startup()
    assert _status(binding)["last_refresh_error_code"] == "refresh_incomplete"
    stdio_server._pull_on_startup()
    assert _status(binding)["last_refresh_error_code"] is None


def test_explicit_sync_uses_same_status(installed):
    _, binding, _, _, _, _, legacy = installed
    result = CliRunner().invoke(app, ["sync", "--project", binding.project_id])
    assert result.exit_code == 0, result.output
    assert _status(binding)["last_refresh_succeeded_at"] is not None
    assert legacy == []


@pytest.mark.parametrize("change", ["actor", "corrupt", "linked"])
def test_status_refuses_other_actor_or_invalid_record(installed, change, tmp_path):
    _, binding, connection, credentials, _, _, _ = installed
    stdio_server._pull_on_startup()
    path = refresh_status._attempt_path(binding, connection, fixtures.USER_ID)
    if change == "actor":
        with credentials.locked():
            credentials.write(
                credentials.read().model_copy(update={"user_id": "01K44444444444444444444444"})
            )
    elif change == "corrupt":
        path.write_bytes(b"PRIVATE INVALID STATUS")
    else:
        target = tmp_path / "private"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    result = refresh_status.replica_status(binding)
    assert result == {
        "project_id": binding.project_id,
        "error_code": "replica_status_unavailable",
        "authorization_checked": False,
    }


@pytest.mark.parametrize("phase", ["start", "success"])
def test_status_persistence_failure_never_reports_success(installed, monkeypatch, caplog, phase):
    _, binding, _, _, _, _, _ = installed
    original = refresh_status.durable_replace

    def failed(paths, path, raw):
        if phase == "start" or json.loads(raw)["error_code"] is None:
            raise OSError("PRIVATE PERSISTENCE DETAIL")
        return original(paths, path, raw)

    monkeypatch.setattr(refresh_status, "durable_replace", failed)
    stdio_server._pull_on_startup()
    status = _status(binding)
    assert status["last_refresh_succeeded_at"] is None
    assert "session-start refresh: incomplete" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_corrupt_credentials_refuse_sync_without_private_details(installed):
    _, binding, _, credentials, _, _, _ = installed
    credentials.path.write_bytes(b'{"private":"PRIVATE CREDENTIAL DETAIL"}')
    result = CliRunner().invoke(app, ["sync", "--project", binding.project_id])
    assert result.exit_code == 1
    assert "Check generation login and project connection" in result.output
    assert "PRIVATE" not in result.output


def test_status_does_not_call_legacy_credentials_or_network(installed, monkeypatch):
    _, binding, _, _, _, calls, _ = installed
    monkeypatch.setattr("nauro.auth.load_access_token", lambda: pytest.fail("Legacy credentials"))
    before = len(calls)
    assert _status(binding)["generation_id"] == fixtures.GENERATION_ID
    assert len(calls) == before


def test_repeat_attachment_after_startup(installed):
    repo, binding, _, _, _, _, _ = installed
    stdio_server._pull_on_startup()
    before = _status(binding)
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert _status(binding) == before


def test_startup_between_association_writes_preserves_attachment_recovery(tmp_path, monkeypatch):
    from nauro.store.repo_config import repo_config_path
    from nauro.sync import generation_attachment as attachment

    repo, _, _, _, _ = attachment_fixture.__wrapped__(tmp_path, monkeypatch)
    with monkeypatch.context() as fault:
        fault.setattr(
            attachment,
            "save_repo_config",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        assert _run(repo).exit_code == 130
    assert not repo_config_path(repo).exists()
    stdio_server._pull_on_startup()
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert json.loads(repo_config_path(repo).read_text())["id"] == fixtures.PROJECT_ID
