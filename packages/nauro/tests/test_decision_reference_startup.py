import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import decision_reference_startup as startup
from nauro.mcp import stdio_server
from nauro.mcp.decision_reference import INSTRUCTIONS, reference_server
from nauro.sync.decision_profile import load_reference_profile, profile_transport
from nauro.sync.decision_reference_contract import reference_schema

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
ACTOR = "01K" + "0" * 21 + "08"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"user_id": ACTOR, "access_token": "synthetic"}))
    credentials.chmod(0o600)
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoint": "https://probe.example/mcp",
                "project_id": PROJECT,
                "actor_id": ACTOR,
                "credentials_file": str(credentials),
            }
        )
    )
    path.chmod(0o600)
    return path, credentials


def test_default_serve_uses_existing_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(stdio_server, "run_stdio", lambda: calls.append("legacy"))
    monkeypatch.setattr(startup, "run_reference_stdio", lambda *_: pytest.fail("Reference startup"))
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 0
    assert calls == ["legacy"]


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_reference_startup_negotiates_before_serving_and_never_pulls(
    profile, monkeypatch, legacy_schema
):
    path, _ = profile
    methods, clients, served = [], [], []
    original_tools = dict(stdio_server.mcp._tool_manager._tools)
    monkeypatch.setattr(stdio_server, "_pull_on_startup", lambda: pytest.fail("Startup pull"))
    monkeypatch.setattr(stdio_server, "run_stdio", lambda: pytest.fail("Legacy fallback"))

    def wire(request):
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        payload = (
            {"protocolVersion": "2025-06-18"}
            if body["method"] == "initialize"
            else {
                "tools": [
                    {
                        "name": "propose_decision",
                        "inputSchema": {} if legacy_schema else reference_schema(),
                    }
                ]
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": payload})

    def client():
        value = httpx.Client(transport=httpx.MockTransport(wire))
        clients.append(value)
        return value

    monkeypatch.setattr(startup, "httpx", SimpleNamespace(Client=client))

    def run(server, *, transport):
        assert transport == "stdio"
        assert methods == ["initialize", "notifications/initialized", "tools/list"]
        assert list(server._tool_manager._tools) == ["propose_decision"]
        assert server.instructions == f"{INSTRUCTIONS} Project ID: {PROJECT}."
        assert server._tool_manager.get_tool("propose_decision").parameters == reference_schema()
        served.append(server)

    monkeypatch.setattr(FastMCP, "run", run)
    result = CliRunner().invoke(app, ["serve", "--reference-profile", str(path)])
    assert result.exit_code == (1 if legacy_schema else 0), result.output
    assert len(served) == (0 if legacy_schema else 1)
    assert result.stdout == ""
    assert clients[0].is_closed is True
    assert stdio_server.mcp._tool_manager._tools == original_tools


def test_invalid_startup_profile_never_transmits(profile, monkeypatch):
    path, _ = profile
    path.write_text('{"access_token":"do-not-print"}')
    monkeypatch.setattr(startup, "profile_transport", lambda *_: pytest.fail("Transport"))
    result = CliRunner().invoke(app, ["serve", "--reference-profile", str(path)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "do-not-print" not in result.output
    assert "Could not start" in result.stderr


def test_bad_credentials_after_startup_do_not_leak_or_transmit(profile):
    path, credentials = profile
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Network"))) as client:
        transport = profile_transport(load_reference_profile(path), client)
        transport._initialized = True
        server = reference_server(transport)
        credentials.write_text(
            json.dumps({"user_id": ACTOR, "access_token": {"secret": "do-not-print"}})
        )
        tool = server._tool_manager.get_tool("propose_decision")
        with pytest.raises(ToolError, match="Reference credentials unavailable") as caught:
            asyncio.run(tool.run({"project_id": PROJECT, "request_mode": "discover"}))
        assert "do-not-print" not in str(caught.value)


def test_reference_server_instances_do_not_share_tools(profile):
    path, _ = profile
    with httpx.Client() as client:
        transport = profile_transport(load_reference_profile(path), client)
        first, second = reference_server(transport), reference_server(transport)
        first.remove_tool("propose_decision")
        assert list(second._tool_manager._tools) == ["propose_decision"]
        assert stdio_server.mcp._tool_manager.get_tool("get_context") is not None


def test_reference_cli_serves_real_stdio_protocol(profile):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    bootstrap = """
import json
from types import SimpleNamespace
import httpx
from nauro.cli.main import app
from nauro.mcp import decision_reference_startup as startup
from nauro.sync.decision_reference_contract import reference_schema

def wire(request):
    assert str(request.url) == 'https://probe.example/mcp'
    body = json.loads(request.content)
    if body['method'] == 'notifications/initialized':
        return httpx.Response(202)
    if body['method'] == 'initialize':
        result = {'protocolVersion': '2025-06-18'}
    elif body['method'] == 'tools/list':
        result = {'tools': [{'name': 'propose_decision', 'inputSchema': reference_schema()}]}
    else:
        assert body['method'] == 'tools/call'
        assert body['params']['arguments']['request_mode'] == 'discover'
        result = {'content': [{'type': 'text', 'text': json.dumps(
            {'version': 1, 'requests': [], 'next_after': None})}]}
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})

startup.httpx = SimpleNamespace(Client=lambda: httpx.Client(transport=httpx.MockTransport(wire)))
app()
"""

    async def session():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, "serve", "--reference-profile", str(profile[0])],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert initialized.instructions == f"{INSTRUCTIONS} Project ID: {PROJECT}."
                tools = await client.list_tools()
                assert [tool.name for tool in tools.tools] == ["propose_decision"]
                assert tools.tools[0].inputSchema == reference_schema()
                result = await client.call_tool(
                    "propose_decision", {"project_id": PROJECT, "request_mode": "discover"}
                )
                assert result.isError is False
                assert json.loads(result.content[0].text) == {
                    "version": 1,
                    "requests": [],
                    "next_after": None,
                }

    asyncio.run(asyncio.wait_for(session(), timeout=15))
