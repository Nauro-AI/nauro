import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import decision_delivery as delivery
from mcp_server.generations import read_generation_pointer
from nauro.mcp import decision_reference_startup as startup
from tests.conftest import TEST_PROJECT_ID
from tests.test_decision_reference import ORIGIN, probe, signed_transport
from tests.test_judgment_planning import USER_ID

__all__ = ["probe", "signed_transport"]


@pytest.mark.parametrize("lost_mode", ["prepare", "submit"])
def test_startup_connection_recovers_after_restart(probe, tmp_path, monkeypatch, lost_mode):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps({"user_id": USER_ID, "access_token": probe.headers["Authorization"][7:]})
    )
    credentials.chmod(0o600)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoint": ORIGIN + "/mcp",
                "project_id": TEST_PROJECT_ID,
                "actor_id": USER_ID,
                "credentials_file": str(credentials),
            }
        )
    )
    profile.chmod(0o600)
    control = {"lost": False, "observations": [], "modes": []}
    clients = []

    def wire(request):
        body = json.loads(request.content)
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        if body["method"] == "tools/call":
            mode = body["params"]["arguments"].get("request_mode", "prepare")
            control["modes"].append(mode)
            if mode == lost_mode and not control["lost"]:
                control["observations"].append(
                    json.loads(response.json()["result"]["content"][0]["text"])
                )
                control["lost"] = True
                raise httpx.ReadError("lost response", request=request)
        return response

    def client():
        value = httpx.Client(transport=httpx.MockTransport(wire))
        clients.append(value)
        return value

    monkeypatch.setattr(startup, "httpx", SimpleNamespace(Client=client))

    def invoke(server, **arguments):
        tool = server._tool_manager.get_tool("propose_decision")
        return asyncio.run(tool.run({"project_id": TEST_PROJECT_ID, **arguments}))

    def first(server, *, transport):
        assert transport == "stdio"
        content = {
            "title": "Recover a started stdio decision",
            "rationale": "Preserve saved request identity across interrupted stdio connections.",
        }
        with pytest.raises(ToolError, match="No verified result"):
            if lost_mode == "prepare":
                invoke(server, **content)
            else:
                saved = invoke(server, **content)
                invoke(
                    server,
                    request_mode="submit",
                    operation_id=saved["request"]["operation_id"],
                    payload_digest=saved["request"]["payload_digest"],
                )

    monkeypatch.setattr(FastMCP, "run", first)
    startup.run_reference_stdio(profile)
    assert clients[0].is_closed is True
    monkeypatch.setattr(
        delivery, "run_pre_team_judgment", lambda *_: pytest.fail("Recovery executed")
    )

    def restarted(server, *, transport):
        found = invoke(server, request_mode="discover")
        assert found["requests"] == control["observations"]
        saved = found["requests"][0]
        recovered = invoke(
            server,
            request_mode="recover",
            operation_id=saved["request"]["operation_id"],
            payload_digest=saved["request"]["payload_digest"],
        )
        assert recovered == saved
        assert saved["status"] == ("committed" if lost_mode == "submit" else "prepared")

    monkeypatch.setattr(FastMCP, "run", restarted)
    startup.run_reference_stdio(profile)
    assert len(clients) == 2 and clients[1].is_closed is True
    assert control["modes"] == (
        ["prepare", "submit", "discover", "recover"]
        if lost_mode == "submit"
        else ["prepare", "discover", "recover"]
    )
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == (
        1 if lost_mode == "submit" else 0
    )
