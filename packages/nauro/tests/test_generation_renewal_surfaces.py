from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import stdio_server
from nauro.store.generation_refresh_io import refresh_paths
from tests.automatic_renewal import expire, install_provider
from tests.test_generation_decision_routing import route
from tests.test_normal_generation_reads import normal

__all__ = ["normal", "route"]


@pytest.mark.parametrize("surface", ["cli", "stdio", "startup"])
def test_expired_account_recovers_at_operation_boundary(normal, monkeypatch, surface):
    binding, server, connection = normal
    calls = install_provider(monkeypatch, connection)
    expire(connection)
    if surface == "cli":
        result = CliRunner().invoke(app, ["get-raw-file", "state.md", "--project", "Nauro"])
        assert result.exit_code == 0, result.output
        assert "Initial generation" in result.output
    elif surface == "stdio":
        result = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
        assert result.isError is False
        assert "Initial generation" in result.content[0].text
    else:
        monkeypatch.setattr(stdio_server, "resolve_project_binding", lambda *a, **k: binding)
        stdio_server._pull_on_startup()
        result = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
        assert result.isError is False
    assert calls == ["/oauth/token", "/.well-known/jwks.json"]
    assert connection.store().incomplete() is False
    assert connection.store().read().refresh_token == "synthetic-refresh"
    assert {bearer for _, route, _, bearer in server.requests if route.startswith("api.test/")} == {
        "Bearer " + connection.store().read().access_token
    }


@pytest.mark.parametrize("surface", ["read", "sync"])
def test_renewal_does_not_hold_replica_locks(normal, monkeypatch, surface):
    from nauro.store.replica_control import _native_control_lock
    from nauro.sync import generation_refresh_status, generation_renewal

    binding, _, connection = normal
    calls = install_provider(monkeypatch, connection)
    worker = generation_renewal._run_worker
    actor = connection.store().read().user_id
    paths = refresh_paths(binding, actor)
    attempt = generation_refresh_status._attempt_path(binding, connection, actor)

    def inspect(request, timeout):
        with (
            _native_control_lock(paths.store, paths.store / ".replica-control.lock", 0),
            _native_control_lock(attempt.parent, attempt.with_suffix(".lock"), 0),
        ):
            worker(request, timeout)

    monkeypatch.setattr(generation_renewal, "_run_worker", inspect)
    expire(connection)
    if surface == "read":
        assert stdio_server.get_raw_file("state.md", project_id=binding.project_id).isError is False
    else:
        result = CliRunner().invoke(app, ["sync", "--project", "Nauro"])
        assert result.exit_code == 0, result.output
    assert calls == ["/oauth/token", "/.well-known/jwks.json"]


def test_stale_replica_still_requires_explicit_refresh_after_credential_renewal(
    normal, monkeypatch
):
    binding, server, connection = normal
    calls = install_provider(monkeypatch, connection)
    expire(connection)
    server.generation_id = "01K66666666666666666666666"
    result = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert result.isError is True
    assert calls == ["/oauth/token", "/.well-known/jwks.json"]
    assert len(server.requests) == 1


SCRIPT = """
import json, socket, sys
from pathlib import Path
import pytest
from nauro.mcp import read_dispatch, stdio_server
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.generation_connection import selected_connection
from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
from tests.test_sync.test_generation_acquisition import FakeServer
from tests.automatic_renewal import install_provider

_connect = socket.socket.connect
def forbidden(sock, address, *args):
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return _connect(sock, address, *args)  # asyncio self-pipe on Windows
    raise AssertionError('External network forbidden')
socket.socket.connect = forbidden
server = FakeServer({p: s.encode() for p, s in json.loads(Path(sys.argv[1]).read_text()).items()})
read_dispatch.GenerationTransferSession = lambda b: GenerationTransferSession(
    b, server.session.client
)
connection, _ = selected_connection(DEFAULT_AUTH_REDIRECT_URI, sys.argv[2], use_cwd=False)
patch = pytest.MonkeyPatch()
install_provider(patch, connection)
stdio_server.run_stdio()
"""


def test_same_stdio_process_recovers_after_expiry_without_reconnect(normal, tmp_path):
    binding, server, connection = normal
    state = tmp_path / "server.json"
    state.write_text(json.dumps({path: body.decode() for path, body in server.artifacts.items()}))
    root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(root / path)
            for path in ("packages/nauro/src", "packages/nauro-core/src", "packages/nauro")
        ),
    }

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", SCRIPT, str(state), binding.project_id],
            env=env,
            cwd=str(tmp_path),
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            before = await session.call_tool(
                "get_raw_file", {"project_id": binding.project_id, "path": "state.md"}
            )
            assert before.isError is False
            expire(connection)
            after = await session.call_tool(
                "get_raw_file", {"project_id": binding.project_id, "path": "state.md"}
            )
            assert after.isError is False
            assert "Initial generation" in after.content[0].text
            assert connection.store().read().access_token != "normal-generation-token"

    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_cached_decision_session_renews_before_submit_without_replaying_write(route, monkeypatch):
    from nauro.sync import generation_decision as routing
    from tests.test_generation_decision_routing import reference, tool, value

    connection, _ = routing.select_decision_connection()
    calls = install_provider(monkeypatch, connection)
    prepared = value(tool(rationale="Preserve one approved request", title="Renewal"))
    expire(connection)
    route.authority.loss = "submit"
    with pytest.raises(ToolError, match="No verified result"):
        tool(request_mode="submit", **reference(prepared))
    assert route.authority.commits == 1
    assert [r.get("request_mode", "prepare") for r in route.authority.calls] == [
        "prepare",
        "submit",
    ]
    assert calls == ["/oauth/token", "/.well-known/jwks.json"]
    recovered = value(tool(request_mode="recover", **reference(prepared)))
    assert recovered["status"] == "committed"
    assert route.authority.commits == 1
    assert [r.get("request_mode", "prepare") for r in route.authority.calls] == [
        "prepare",
        "submit",
        "recover",
    ]
