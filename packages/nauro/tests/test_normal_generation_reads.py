"""Ordinary reads and explicit replica refresh use one normal credential binding."""

import json
import socket

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import read_dispatch, stdio_server
from nauro.store import generation_installation as installation
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    verify_generation_projection,
)
from nauro.store.registry import register_project_v2
from nauro.store.resolution import resolve_project_binding
from nauro.sync import generation_acquisition, generation_refresh, generation_refresh_status
from nauro.sync.generation_session import GenerationTransferSession
from tests.generation_account import seed_generation_account
from tests.test_sync.test_generation_acquisition import PROJECT_ID, USER_ID, FakeServer


def forbidden(*_args, **_kwargs):
    pytest.fail("Legacy, automatic or external path executed")


@pytest.fixture
def normal(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    register_project_v2(
        "Nauro", [], project_id=PROJECT_ID, mode="cloud", server_url="https://api.test"
    )
    binding = resolve_project_binding(PROJECT_ID, None)
    binding.store_path.mkdir(parents=True, exist_ok=True)
    server = FakeServer(
        {"project.md": b"# Project\n", "state.md": b"# State\nInitial generation\n"}
    )
    projection = verify_generation_projection(
        GenerationProjectionTarget(binding, GenerationProjectionIdentity(**server.identity())),
        manifest_json=server.envelope(),
        artifacts=tuple(server.artifacts.items()),
    )
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    installation.publish_generation_control(installation.install_generation_root(projection))
    connection = seed_generation_account(binding, USER_ID, monkeypatch)
    monkeypatch.setattr(installation, "read_active_user_id", forbidden)
    monkeypatch.setattr(generation_acquisition, "with_token_refresh", forbidden)

    def factory(b):
        return GenerationTransferSession(b, server.session.client)

    monkeypatch.setattr(read_dispatch, "GenerationTransferSession", factory)
    monkeypatch.setattr(generation_refresh_status, "GenerationTransferSession", factory)
    with factory(binding) as session:
        generation_refresh.commit_generation_refresh(
            generation_refresh.prepare_initial_generation_refresh(
                binding, actor=USER_ID, session=session
            ),
            session=session,
        )
    for name in read_dispatch.GENERATION_READS:
        monkeypatch.setattr(read_dispatch.legacy, f"tool_{name}", forbidden)
    server.requests.clear()
    yield binding, server, connection
    server.session.client.close()


def test_cli_and_full_stdio_read_verified_generation(normal):
    binding, server, _ = normal
    (binding.store_path / "state.md").write_text("STALE FLAT CONTENT")
    response = stdio_server.get_raw_file("state.md", project_id=PROJECT_ID)
    assert response.isError is False
    assert "Initial generation" in response.content[0].text
    assert "STALE" not in response.content[0].text
    assert server.generation_id in response.content[0].text
    result = CliRunner().invoke(app, ["get-raw-file", "state.md", "--project", "Nauro"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["read_authority"]["generation_id"] == server.generation_id
    assert "Initial generation" in result.stdout
    assert "STALE" not in result.stdout
    text = CliRunner().invoke(
        app, ["get-raw-file", "state.md", "--project", "Nauro", "--format", "text"]
    )
    assert text.exit_code == 0, text.output
    assert "Authorization checked for this read." in text.stdout
    assert {bearer for _, route, _, bearer in server.requests if route.startswith("api.test/")} == {
        "Bearer normal-generation-token"
    }


def test_explicit_sync_refreshes_and_never_writes_flat_store_or_runs_hooks(normal, monkeypatch):
    from nauro.cli.commands import sync as sync_command

    binding, server, _ = normal
    flat = binding.store_path / "state.md"
    flat.write_text("legacy retained")
    server.generation_id = "01K66666666666666666666666"
    server.artifacts["state.md"] = b"# State\nNew committed generation\n"
    assert stdio_server.get_raw_file("state.md", project_id=PROJECT_ID).isError is True
    for name in ("capture_snapshot", "warn_then_regen", "push_store_to_cloud", "_pull_from_cloud"):
        monkeypatch.setattr(sync_command, name, forbidden)
    result = CliRunner().invoke(app, ["sync", "--project", "Nauro"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"Refreshed generation {server.generation_id}.\n"
    response = stdio_server.get_raw_file("state.md", project_id=PROJECT_ID)
    assert response.isError is False
    assert "New committed generation" in response.content[0].text
    assert flat.read_text() == "legacy retained"
    assert all(
        bearer is None
        for _, route, _, bearer in server.requests
        if route.startswith("objects.test/")
    )
    assert all("/oauth/token" not in route for _, route, _, _ in server.requests)


@pytest.mark.parametrize("change", ["expired", "logout", "actor", "marker", "endpoint"])
def test_changed_authority_refuses_without_legacy_fallback(normal, change):
    from nauro.store.config import load_config, save_config

    binding, server, connection = normal
    if change in {"expired", "logout", "actor"}:
        store = connection.store()
        with store.locked():
            before = store.read()
            changes = {
                "expired": {"expires_at": 1},
                "logout": {"state": "logged_out"},
                "actor": {"user_id": "01K44444444444444444444444"},
            }[change]
            store.write(before.model_copy(update=changes))
    elif change == "marker":
        (binding.store_path / ".replica/authority.json").write_text("corrupt")
    else:
        config = load_config()
        config["api_url"] = "https://untrusted.test"
        save_config(config)
    assert stdio_server.get_raw_file("state.md", project_id=PROJECT_ID).isError is True
    assert (
        CliRunner().invoke(app, ["get-raw-file", "state.md", "--project", "Nauro"]).exit_code == 1
    )
    assert server.requests == []


def test_401_does_not_refresh_or_repeat_request(normal):
    _, server, _ = normal
    server.projection_hook = lambda *_: httpx.Response(401)
    result = stdio_server.get_raw_file("state.md", project_id=PROJECT_ID)
    assert result.isError is True
    assert len(server.requests) == 1
    assert server.requests[0][1] == "api.test/generations/projection"


def test_logout_during_projection_discards_response(normal):
    _, server, connection = normal

    def logout(_count, payload):
        store = connection.store()
        with store.locked():
            store.write(store.empty("logged_out"))
        return payload

    server.projection_hook = logout
    result = stdio_server.get_raw_file("state.md", project_id=PROJECT_ID)
    assert result.isError is True
    assert "Initial generation" not in str(result)
    assert len(server.requests) == 1


def test_missing_refresh_intent_does_not_bootstrap_automatically(normal):
    binding, server, _ = normal
    (binding.store_path / f".replica/v1/actors/{USER_ID}/refresh-intent.json").unlink()
    result = CliRunner().invoke(app, ["sync", "--project", "Nauro"])
    assert result.exit_code == 1
    assert "bootstrap" in result.output
    assert server.requests == []


def test_push_only_is_refused_for_generation_replica(normal):
    _, server, _ = normal
    result = CliRunner().invoke(app, ["sync", "--project", "Nauro", "--push-only"])
    assert result.exit_code == 1
    assert "cannot push local files" in result.output
    assert server.requests == []


def test_partial_refresh_requires_explicit_recovery_and_keeps_old_evidence(normal, monkeypatch):
    binding, server, _ = normal
    server.generation_id = "01K66666666666666666666666"
    server.artifacts["state.md"] = b"# State\nRecovered generation\n"
    replace = generation_refresh.durable_replace
    failed = []

    def interrupt(paths, path, raw):
        if path == paths.pointer and not failed:
            failed.append(True)
            raise OSError("Synthetic pointer publication failure")
        return replace(paths, path, raw)

    monkeypatch.setattr(generation_refresh, "durable_replace", interrupt)
    result = CliRunner().invoke(app, ["sync", "--project", "Nauro"])
    assert result.exit_code == 1
    assert failed == [True]
    paths = generation_refresh.refresh_paths(binding, USER_ID)
    intent = paths.intent.read_bytes()
    retained = sorted(paths.history.glob("*.json"))
    assert len(retained) == 1
    assert stdio_server.get_raw_file("state.md", project_id=PROJECT_ID).isError is True
    assert paths.intent.read_bytes() == intent
    result = CliRunner().invoke(app, ["sync", "--project", "Nauro"])
    assert result.exit_code == 0, result.output
    assert sorted(paths.history.glob("*.json")) == retained
    assert paths.intent.read_bytes() == intent
    assert (
        "Recovered generation"
        in stdio_server.get_raw_file("state.md", project_id=PROJECT_ID).content[0].text
    )


def test_full_stdio_process_reopens_normal_credentials_and_replica(normal, tmp_path):
    import asyncio
    import os
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    binding, server, connection = normal
    state = tmp_path / "server.json"
    state.write_text(json.dumps({path: body.decode() for path, body in server.artifacts.items()}))
    source = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(p)
            for p in (
                source / "packages/nauro/src",
                source / "packages/nauro-core/src",
                source / "packages/nauro",
            )
        ),
    }
    script = """
import json, socket, sys
from pathlib import Path
from tests.test_sync.test_generation_acquisition import FakeServer
from nauro.mcp import read_dispatch
from nauro.sync.generation_session import GenerationTransferSession
from nauro.mcp.stdio_server import run_stdio
import nauro.sync.hooks


def forbidden(*args, **kwargs):
    raise AssertionError("External connection or startup pull forbidden")


socket.socket.connect = forbidden
nauro.sync.hooks.pull_before_session = forbidden
server = FakeServer(
    {p: text.encode() for p, text in json.loads(Path(sys.argv[1]).read_text()).items()}
)
read_dispatch.GenerationTransferSession = lambda b: GenerationTransferSession(
    b, server.session.client
)
run_stdio()
"""

    async def exercise(error):
        params = StdioServerParameters(
            command=sys.executable, args=["-c", script, str(state)], env=env, cwd=str(tmp_path)
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 10
            result = await session.call_tool(
                "get_raw_file", {"project_id": PROJECT_ID, "path": "state.md"}
            )
            assert result.isError is error
            assert ("Initial generation" in result.content[0].text) is (not error)

    asyncio.run(exercise(False))
    store = connection.store()
    with store.locked():
        store.write(store.empty("logged_out"))
    asyncio.run(exercise(True))


@pytest.mark.parametrize("changed_scope", [False, True])
def test_ordinary_history_uses_normal_credentials_and_verifies_scope(
    normal, monkeypatch, changed_scope
):
    from nauro.sync import history_transport
    from tests.test_history_transport import response_body

    binding, server, _ = normal
    target = GenerationProjectionTarget(binding, GenerationProjectionIdentity(**server.identity()))
    calls = []

    def wire(request):
        if request.url.path == "/generations/history":
            calls.append(request)
            body = response_body(target)
            if changed_scope:
                body["projection_scope_id"] = "b" * 64
            return httpx.Response(200, json=body)
        return server.handle(request)

    monkeypatch.setattr(history_transport, "read_active_credentials", forbidden)
    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        monkeypatch.setattr(
            read_dispatch,
            "GenerationTransferSession",
            lambda b: GenerationTransferSession(b, client),
        )
        result = stdio_server.diff_since_last_session(project_id=PROJECT_ID)
    assert result.isError is changed_scope
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer normal-generation-token"
    if not changed_scope:
        assert result.content[0].text == response_body(target)["text"]
    else:
        assert "Removed file" not in result.content[0].text
