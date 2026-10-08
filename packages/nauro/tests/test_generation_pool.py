from dataclasses import replace
from unittest.mock import Mock

import anyio
import httpx
import pytest

from nauro.mcp import stdio_server
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync import generation_pool as pool
from nauro.sync.generation_credentials import GenerationConnection


class Transport(httpx.MockTransport):
    def __init__(self):
        self.requests = []
        self.closes = 0
        super().__init__(self.handle)

    def handle(self, request):
        assert self.closes == 0
        self.requests.append(request)
        return httpx.Response(200, content=b"verified", headers={"Set-Cookie": "session=private"})

    def close(self):
        self.closes += 1


@pytest.fixture
def wire(tmp_path, monkeypatch):
    binding = ResolvedProjectBinding(tmp_path, "project", "Project", "cloud", "https://api.test")
    connection = GenerationConnection(
        endpoint="https://api.test/mcp",
        issuer="https://issuer.test/",
        client_id="client",
        audience="audience",
        redirect_uri="http://localhost:18457/callback",
    )
    transports = []

    def create(**kwargs):
        assert kwargs == {"trust_env": False}
        transport = Transport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(pool.httpx, "HTTPTransport", create)
    return binding, connection, transports


def test_clients_reuse_only_transport(wire):
    binding, connection, transports = wire
    with pool.reuse_generation_connections():
        with pool.generation_client(binding, connection, "actor") as first:
            first.headers["Authorization"] = "Bearer first"
            assert first.get("https://api.test/read").content == b"verified"
            assert first.cookies["session"] == "private"
        assert first.is_closed is True
        assert transports[0].closes == 0
        with pool.generation_client(binding, connection, "actor") as second:
            assert second.get("https://api.test/read").content == b"verified"
        assert len(transports) == 1
        assert transports[0].requests[1].headers.get("authorization") is None
        assert transports[0].requests[1].headers.get("cookie") is None
    assert transports[0].closes == 1


@pytest.mark.parametrize("change", ["actor", "endpoint", "issuer", "project", "path"])
def test_changed_binding_has_separate_transport(wire, change):
    binding, connection, transports = wire
    other_binding, other_connection, actor = binding, connection, "actor"
    if change == "actor":
        actor = "other"
    elif change in {"endpoint", "issuer"}:
        value = "https://other.test/mcp" if change == "endpoint" else "https://other.test/"
        other_connection = connection.model_copy(update={change: value})
    elif change == "project":
        other_binding = replace(binding, project_id="other")
    else:
        other_binding = replace(binding, store_path=binding.store_path / "other")
    with (
        pool.reuse_generation_connections(),
        pool.generation_client(binding, connection, "actor") as first,
    ):
        with pool.generation_client(other_binding, other_connection, actor) as second:
            assert second.get("https://api.test/read").status_code == 200
        assert first.get("https://api.test/read").status_code == 200
        assert len(transports) == 2
        assert [t.closes for t in transports] == [0, 0]
    assert [t.closes for t in transports] == [1, 1]


def test_shutdown_preserves_active_borrower_and_closes_once(wire):
    binding, connection, transports = wire
    with pool.reuse_generation_connections():
        active = pool.generation_client(binding, connection, "actor")
        idle = pool.generation_client(binding, connection, "other")
        idle.close()
    assert [t.closes for t in transports] == [0, 1]
    assert active.get("https://api.test/read").content == b"verified"
    active.close()
    active.close()
    assert [t.closes for t in transports] == [1, 1]


def test_closed_scope_refuses_inherited_context(wire):
    from contextvars import copy_context

    binding, connection, transports = wire
    with pool.reuse_generation_connections():
        context = copy_context()
    with pytest.raises(RuntimeError, match="pool is closed"):
        context.run(pool.generation_client, binding, connection, "actor")
    assert transports == []


def test_client_construction_failure_releases_borrower(wire, monkeypatch):
    binding, connection, transports = wire
    monkeypatch.setattr(pool.httpx, "Client", Mock(side_effect=ValueError("invalid client")))
    with pytest.raises(ValueError, match="invalid client"), pool.reuse_generation_connections():
        pool.generation_client(binding, connection, "actor")
    assert transports[0].closes == 1


@pytest.mark.parametrize("failure", [False, True])
def test_stdio_scope_covers_startup_and_concurrent_worker_calls(wire, monkeypatch, failure):
    binding, connection, transports = wire

    def request():
        with pool.generation_client(binding, connection, "actor") as client:
            assert client.get("https://api.test/read").content == b"verified"

    async def concurrent():
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.to_thread.run_sync, request)
            group.start_soon(anyio.to_thread.run_sync, request)

    def run(**kwargs):
        assert kwargs == {"transport": "stdio"}
        anyio.run(concurrent)
        if failure:
            raise ValueError("server failed")

    monkeypatch.setattr(stdio_server, "_pull_on_startup", request)
    monkeypatch.setattr(stdio_server.mcp, "run", run)
    close = Mock()
    monkeypatch.setattr(stdio_server.decision_session, "close", close)
    if failure:
        with pytest.raises(ValueError, match="server failed"):
            stdio_server.run_stdio()
    else:
        stdio_server.run_stdio()
    assert len(transports) == 1
    assert len(transports[0].requests) == 3
    assert transports[0].closes == 1
    close.assert_called_once_with()


def test_idle_cache_is_bounded_and_keeps_recently_used_binding(wire):
    binding, connection, transports = wire
    with pool.reuse_generation_connections():
        for actor in range(8):
            with pool.generation_client(binding, connection, str(actor)):
                pass
        with pool.generation_client(binding, connection, "0"):
            pass
        with pool.generation_client(binding, connection, "8"):
            pass
        assert len(transports) == 9
        assert [t.closes for t in transports] == [0, 1, 0, 0, 0, 0, 0, 0, 0]
        with pool.generation_client(binding, connection, "0"):
            pass
        assert len(transports) == 9
        with pool.generation_client(binding, connection, "1"):
            pass
        assert len(transports) == 10
        assert transports[2].closes == 1
    assert [t.closes for t in transports] == [1] * 10


def test_idle_eviction_never_closes_active_borrowers(wire):
    binding, connection, transports = wire
    with pool.reuse_generation_connections():
        first = pool.generation_client(binding, connection, "active")
        second = pool.generation_client(binding, connection, "active")
        for actor in range(20):
            with pool.generation_client(binding, connection, str(actor)):
                pass
        assert len(transports) == 21
        assert [t.closes for t in transports] == [0] + [1] * 12 + [0] * 8
        first.close()
        assert second.get("https://api.test/read").status_code == 200
        assert transports[0].closes == 0
        second.close()
        assert [t.closes for t in transports] == [0] + [1] * 13 + [0] * 7
    assert [t.closes for t in transports] == [1] * 21
