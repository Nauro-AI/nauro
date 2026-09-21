from __future__ import annotations

import base64
import json
from unittest.mock import Mock

import httpx
import pytest

from nauro.mcp import stdio_server
from nauro.store.generation_refresh_io import refresh_paths
from nauro.sync import generation_decision as routing
from nauro.sync import generation_refresh_status as status
from nauro.sync.generation_session import GenerationConnectionError
from tests import test_generation_installation as fixtures
from tests.test_generation_decision_routing import cli, reference, route, tool, value
from tests.test_stdio_startup_authority import installed

__all__ = ["installed", "route"]


@pytest.mark.parametrize("mode", ["submit", "retry", "recover"])
def test_committed_receipt_refreshes_then_reads_new_generation(installed, monkeypatch, mode):
    _, binding, connection, _, _, _, legacy = installed
    monkeypatch.setattr(fixtures, "GENERATION_ID", "01K77777777777777777777777")
    projection = fixtures._projection({"state.md": b"New committed state\n"})
    with httpx.Client(trust_env=False) as client:
        concrete = type(client)

    def wire(request):
        if request.url.path == "/generations/projection":
            return httpx.Response(
                200,
                json={
                    "projection": projection.target.identity.model_dump(),
                    "manifest_base64": base64.b64encode(projection.manifest_json).decode(),
                },
            )
        if request.url.path == "/generations/presign":
            return httpx.Response(
                200,
                json={
                    "projection": projection.target.identity.model_dump(),
                    "urls": [{"path": "state.md", "url": "https://objects.example/state"}],
                    "expires_at": "2999-12-31T23:59:59Z",
                },
            )
        assert request.url.host == "objects.example"
        return httpx.Response(200, content=b"New committed state\n")

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: concrete(transport=httpx.MockTransport(wire), **kw)
    )
    committed = {"status": "committed", "execution": {"receipt_json": '{"verified":"receipt"}'}}
    transport = Mock(return_value=committed)
    monkeypatch.setattr(routing.DecisionReferenceTransport, "propose_decision", transport)
    assert stdio_server.get_raw_file("state.md", project_id=binding.project_id).isError is True
    result = routing.execute_decision((connection, binding.project_id), {"request_mode": mode})
    assert result["execution"] == committed["execution"]
    assert result["status"] == "committed"
    assert result["replica_status"]["generation_id"] == fixtures.GENERATION_ID
    assert result["replica_status"]["last_refresh_error_code"] is None
    assert result["replica_status"]["last_refresh_succeeded_at"] is not None
    read = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert read.isError is False
    assert "New committed state" in read.content[0].text
    transport.assert_called_once_with(request_mode=mode)
    assert legacy == []


def test_cli_commit_remains_success_when_refresh_fails(route):
    draft = json.loads(cli("Synthetic draft", "--title", "Draft").stdout)
    ref = reference(draft)
    result = cli(
        "--request-mode",
        "submit",
        "--operation-id",
        ref["operation_id"],
        "--payload-digest",
        ref["payload_digest"],
    )
    assert result.exit_code == 0, result.output
    committed = json.loads(result.stdout)
    assert committed["status"] == "committed"
    assert committed["replica_status"]["error_code"] == "receipt_refresh_required"
    recovered = value(tool(request_mode="recover", **ref))
    assert recovered["execution"]["receipt_json"] == committed["execution"]["receipt_json"]
    assert route.authority.commits == 1


@pytest.mark.parametrize("mode", ["prepare", "discover", "recover", "submit"])
def test_noncommitted_observations_do_not_refresh(route, monkeypatch, mode):
    draft = value(tool(rationale="First", title="First"))
    second = value(tool(rationale="Second", title="Second"))
    if mode == "submit":
        tool(request_mode="submit", **reference(second))
    refresh = Mock(side_effect=AssertionError("noncommitted refresh"))
    monkeypatch.setattr(routing, "refresh_replica", refresh)
    args = (
        {"rationale": "Another draft"}
        if mode == "prepare"
        else ({} if mode == "discover" else reference(draft))
    )
    result = value(tool(request_mode=mode, **args))
    assert "replica_status" not in result
    refresh.assert_not_called()


@pytest.mark.parametrize("changed", ["actor", "endpoint"])
def test_refresh_rejects_different_receipt_binding_before_local_writes(
    installed, monkeypatch, changed
):
    _, binding, connection, _, _, calls, _ = installed
    pointer = refresh_paths(binding, fixtures.USER_ID).pointer
    before = pointer.read_bytes()
    actor = "01K88888888888888888888888" if changed == "actor" else fixtures.USER_ID
    expected_connection = connection
    if changed == "endpoint":
        expected_connection = connection.model_copy(
            update={"endpoint": "https://other.example/mcp"}
        )
    calls.clear()
    with pytest.raises(GenerationConnectionError, match="refresh account changed"):
        status.refresh_replica(binding, expected=(expected_connection, actor))
    assert pointer.read_bytes() == before
    assert calls == []
