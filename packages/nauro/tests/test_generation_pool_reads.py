import httpx
import pytest

from nauro.mcp import read_dispatch, stdio_server
from nauro.store.config import load_config, save_config
from nauro.sync import generation_guidance, generation_pool, generation_refresh_status
from nauro.sync.generation_session import GenerationConnectionError, GenerationTransferSession
from tests.automatic_renewal import expire, install_provider
from tests.test_normal_generation_reads import normal

__all__ = ["normal"]


@pytest.fixture
def pooled(normal, monkeypatch):
    binding, server, connection = normal
    transports = []

    def transport(**kwargs):
        value = httpx.MockTransport(server.handle)
        transports.append(value)
        return value

    monkeypatch.setattr(generation_pool.httpx, "HTTPTransport", transport)
    for module in (read_dispatch, generation_guidance, generation_refresh_status):
        monkeypatch.setattr(module, "GenerationTransferSession", GenerationTransferSession)
    return binding, server, connection, transports


def test_startup_guidance_and_reads_share_transport_with_fresh_credentials(pooled, monkeypatch):
    binding, server, connection, transports = pooled
    monkeypatch.setattr(stdio_server, "resolve_project_binding", lambda *a, **k: binding)
    calls = install_provider(monkeypatch, connection)
    with generation_pool.reuse_generation_connections():
        stdio_server._pull_on_startup()
        first = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
        assert first.isError is False
        expire(connection)
        server.requests.clear()
        second = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
        assert second.isError is False
        assert second.content == first.content
        assert calls == ["/oauth/token", "/.well-known/jwks.json"]
        assert {
            bearer for _, route, _, bearer in server.requests if route.startswith("api.test/")
        } == {"Bearer " + connection.store().read().access_token}
        assert len(transports) == 1
        server.generation_id = "01K66666666666666666666666"
        assert stdio_server.get_raw_file("state.md", project_id=binding.project_id).isError is True


def test_session_circuits_remain_operation_local(pooled):
    binding, _, _, transports = pooled
    with generation_pool.reuse_generation_connections():
        with GenerationTransferSession(binding) as first:
            first.trip(first.api_url)
        with GenerationTransferSession(binding) as second:
            assert second.guard(second.api_url, "manifest") == "https://api.test:443"
        assert len(transports) == 1


def test_injected_client_remains_caller_owned(pooled):
    binding, server, _, transports = pooled
    with generation_pool.reuse_generation_connections():
        with GenerationTransferSession(binding, server.session.client):
            pass
        assert server.session.client.is_closed is False
        assert transports == []


def test_standalone_sessions_close_their_own_clients(pooled):
    binding, _, _, transports = pooled
    with GenerationTransferSession(binding) as first:
        assert first.client.is_closed is False
    assert first.client.is_closed is True
    with GenerationTransferSession(binding) as second:
        assert second.client is not first.client
    assert second.client.is_closed is True
    assert transports == []


@pytest.mark.parametrize("change", ["logout", "actor", "endpoint"])
def test_old_and_new_operations_refuse_changed_authority(pooled, change):
    binding, server, connection, transports = pooled
    with generation_pool.reuse_generation_connections():
        assert stdio_server.get_raw_file("state.md", project_id=binding.project_id).isError is False
        with GenerationTransferSession(binding) as prior:
            if change == "endpoint":
                config = load_config()
                config["api_url"] = "https://other.test"
                save_config(config)
            else:
                store = connection.store()
                with store.locked():
                    changes = (
                        {"state": "logged_out"}
                        if change == "logout"
                        else {"user_id": "01K44444444444444444444444"}
                    )
                    store.write(store.read().model_copy(update=changes))
            server.requests.clear()
            with pytest.raises(GenerationConnectionError):
                prior.credentials()
            assert (
                stdio_server.get_raw_file("state.md", project_id=binding.project_id).isError is True
            )
            assert server.requests == []
