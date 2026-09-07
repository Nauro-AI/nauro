import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from nauro.auth import ActiveCredentials
from nauro.mcp.decision_reference import reference_server
from nauro.sync.decision_reference import DecisionReferenceError, DecisionReferenceTransport
from nauro.sync.decision_reference_contract import MAX_RESPONSE, reference_schema
from nauro.sync.reference_reads import (
    READ_SPECS,
    negotiate_registry,
    read_schema,
    verify_read_result,
)

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
ACTOR = "01K" + "0" * 21 + "08"
TEXT = (
    "# Synthetic decision\n\nGeneration: synthetic-generation. Committed: synthetic-time.\n"
    "Authorization checked for this read."
)


def registry(expanded=True):
    tools = [{"name": "propose_decision", "inputSchema": reference_schema()}]
    if expanded:
        tools.extend({"name": name, "inputSchema": read_schema(name)} for name in READ_SPECS)
    return {"tools": tools}


@pytest.fixture
def wire():
    calls = []
    control = {
        "registry": registry(),
        "result": {"content": [{"type": "text", "text": TEXT}], "isError": False},
        "token": "first",
        "status": 200,
        "rpc_id_delta": 0,
    }

    def send(request):
        body = json.loads(request.content)
        calls.append((body, request.headers["authorization"]))
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif body["method"] == "tools/list":
            result = control["registry"]
        else:
            if control["status"] == "lost":
                raise httpx.ReadError("lost", request=request)
            result = control["result"]
            if control["status"] != 200:
                return httpx.Response(control["status"])
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"] + control["rpc_id_delta"], "result": result},
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        transport = DecisionReferenceTransport(
            "https://probe.example/mcp",
            PROJECT,
            ACTOR,
            client,
            lambda: ActiveCredentials(ACTOR, control["token"]),
        )
        yield SimpleNamespace(control=control, calls=calls, transport=transport)


@pytest.mark.parametrize("expanded", [False, True])
def test_exact_registry_controls_local_tools(wire, expanded):
    wire.control["registry"] = registry(expanded)
    wire.transport.initialize()
    server = reference_server(wire.transport)
    assert wire.transport.reads_available is expanded
    assert set(server._tool_manager._tools) == {t["name"] for t in registry(expanded)["tools"]}
    for spec in registry(expanded)["tools"]:
        assert server._tool_manager.get_tool(spec["name"]).parameters == spec["inputSchema"]
    assert server._tool_manager.get_tool("propose_decision").parameters == reference_schema()


def test_registry_order_does_not_change_admission():
    value = registry()
    value["tools"].reverse()
    assert negotiate_registry(value) is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"tools": []},
        {"tools": "bad"},
        {"tools": [None]},
        {"tools": [{"name": []}]},
        {"tools": registry()["tools"][:2]},
        {"tools": registry()["tools"] + [registry()["tools"][0]]},
        {"tools": [registry()["tools"][0]] * 3},
        {**registry(), "nextCursor": "more"},
        {**registry(), "nextCursor": None},
    ],
)
def test_incomplete_or_malformed_registry_is_refused(wire, bad):
    wire.control["registry"] = bad
    with pytest.raises(DecisionReferenceError, match="registry"):
        wire.transport.initialize()
    assert wire.transport.reads_available is False
    assert wire.transport._initialized is False
    assert [c[0]["method"] for c in wire.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize("mutation", ["name", "schema", "proposal"])
def test_unknown_or_changed_schema_is_refused(mutation):
    value = registry()
    if mutation == "name":
        value["tools"][2]["name"] = "delete_file"
    else:
        index = 0 if mutation == "proposal" else 1
        value["tools"][index]["inputSchema"]["required"] = []
    with pytest.raises(DecisionReferenceError):
        negotiate_registry(value)


def test_singleton_read_is_refused_without_tool_call(wire):
    wire.control["registry"] = registry(False)
    with pytest.raises(DecisionReferenceError, match="does not expose"):
        wire.transport.read("get_context", project_id=PROJECT)
    assert len(wire.calls) == 3


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("get_context", {}),
        ("get_context", {"project_id": "other"}),
        ("get_context", {"project_id": PROJECT, "extra": True}),
        ("get_context", {"project_id": PROJECT, "level": 1}),
        ("get_context", {"project_id": PROJECT, "level": "l0"}),
        ("get_context", {"project_id": PROJECT, "level": None}),
        ("get_decision", {"project_id": PROJECT}),
        ("get_decision", {"project_id": PROJECT, "number": True}),
        ("get_decision", {"project_id": PROJECT, "number": "1"}),
        ("get_decision", {"project_id": PROJECT, "number": 1.0}),
        ("get_decision", {"project_id": PROJECT, "number": 1, "mode": "other"}),
    ],
)
def test_invalid_reads_refuse_before_network_and_before_mcp_coercion(wire, name, arguments):
    with pytest.raises(DecisionReferenceError):
        wire.transport.read(name, **arguments)
    assert wire.calls == []
    wire.transport.reads_available = True
    tool = reference_server(wire.transport)._tool_manager.get_tool(name)
    with pytest.raises(ToolError):
        asyncio.run(tool.run(arguments))
    assert wire.calls == []


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize(
    "name,arguments",
    [("get_context", {"level": "L2"}), ("get_decision", {"number": 4, "mode": "header"})],
)
def test_read_preserves_exact_text_and_error_flag_and_reloads_credentials(
    wire, is_error, name, arguments
):
    wire.control["result"]["isError"] = is_error
    wire.transport.initialize()
    tool = reference_server(wire.transport)._tool_manager.get_tool(name)
    wire.control["token"] = "renewed"
    result = asyncio.run(tool.run({"project_id": PROJECT, **arguments}))
    assert result.isError is is_error
    assert len(result.content) == 1
    assert result.content[0].text == TEXT
    assert wire.calls[-1] == (
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"project_id": PROJECT, **arguments}},
        },
        "Bearer renewed",
    )
    assert len(wire.calls) == 4


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"content": [], "isError": False},
        {"content": [{"type": "text", "text": "bad"}]},
        {"content": [{"type": "text", "text": "bad"}], "isError": "false"},
        {"content": [{"type": "image", "data": "bad"}], "isError": False},
        {"content": [{"type": "text", "text": 5}], "isError": False},
        {"content": [{"type": "text", "text": "bad", "extra": 1}], "isError": False},
        {"content": [{"type": "text", "text": "bad"}], "isError": False, "structuredContent": {}},
    ],
)
def test_malformed_read_results_are_not_forwarded(wire, bad):
    wire.control["result"] = bad
    with pytest.raises(DecisionReferenceError, match="No verified read result"):
        wire.transport.read("get_context", project_id=PROJECT)
    assert len(wire.calls) == 4


def test_text_and_wire_size_limits_are_enforced(wire):
    value = {"content": [{"type": "text", "text": "x" * (MAX_RESPONSE + 1)}], "isError": False}
    with pytest.raises(DecisionReferenceError):
        verify_read_result(value)
    wire.control["result"] = value
    with pytest.raises(DecisionReferenceError, match="No verified read result"):
        wire.transport.read("get_context", project_id=PROJECT)
    assert len(wire.calls) == 4


@pytest.mark.parametrize("failure", [401, 403, 503, "lost", "id"])
def test_failed_read_has_no_reissue(wire, failure):
    wire.transport.initialize()
    if failure == "id":
        wire.control["rpc_id_delta"] = 1
    else:
        wire.control["status"] = failure
    with pytest.raises(DecisionReferenceError, match="No verified read result"):
        wire.transport.read("get_decision", project_id=PROJECT, number=4)
    assert len(wire.calls) == 4


@pytest.mark.parametrize("version", [1, 2])
def test_negotiated_reads_over_real_stdio(version, tmp_path):
    import sys
    import time

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from nauro.sync.decision_profile import RenewalProfile
    from nauro.sync.reference_auth import ReferenceAuth

    tmp_path.chmod(0o700)
    credentials = tmp_path / "credentials.json"
    config = {
        "version": version,
        "endpoint": "https://probe.example/mcp",
        "project_id": PROJECT,
        "actor_id": ACTOR,
        "credentials_file": str(credentials),
    }
    if version == 1:
        credentials.write_text(json.dumps({"user_id": ACTOR, "access_token": "synthetic"}))
        credentials.chmod(0o600)
    else:
        config.update(
            issuer="https://issuer.example/",
            client_id="synthetic-client",
            audience="https://probe.example/mcp",
            expected_subject="synthetic-owner",
            redirect_uri="http://127.0.0.1:18765/callback",
        )
        with httpx.Client() as client:
            auth = ReferenceAuth(RenewalProfile.model_validate(config), client)
            with auth.store.locked():
                auth.store.write(
                    auth._record(("synthetic", "synthetic-refresh", int(time.time()) + 600))
                )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    bootstrap = """
import json
from types import SimpleNamespace
import httpx
from nauro.cli.main import app
from nauro.mcp import decision_reference_startup as startup
from nauro.sync.decision_reference_contract import reference_schema
from nauro.sync.reference_reads import READ_SPECS, read_schema

def wire(request):
    assert request.headers['authorization'] == 'Bearer synthetic'
    body = json.loads(request.content)
    if body['method'] == 'notifications/initialized':
        return httpx.Response(202)
    if body['method'] == 'initialize':
        result = {'protocolVersion': '2025-06-18'}
    elif body['method'] == 'tools/list':
        result = {'tools': [{'name': 'propose_decision', 'inputSchema': reference_schema()}] + [
            {'name': name, 'inputSchema': read_schema(name)} for name in READ_SPECS]}
    else:
        assert body['method'] == 'tools/call'
        name = body['params']['name']
        assert name in READ_SPECS
        result = {'content': [{'type': 'text', 'text': 'Exact génération text.\\n'}],
                  'isError': name == 'get_context'}
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})
startup.httpx = SimpleNamespace(Client=lambda: httpx.Client(transport=httpx.MockTransport(wire)))
app()
"""

    async def session():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, "serve", "--reference-profile", str(path)],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert {tool.name: tool.inputSchema for tool in listed.tools} == {
                    tool["name"]: tool["inputSchema"] for tool in registry()["tools"]
                }
                context = await client.call_tool("get_context", {"project_id": PROJECT})
                decision = await client.call_tool(
                    "get_decision", {"project_id": PROJECT, "number": 1}
                )
                assert context.isError is True
                assert decision.isError is False
                assert context.content == decision.content
                assert decision.content[0].text == "Exact génération text.\n"

    asyncio.run(asyncio.wait_for(session(), timeout=15))


@pytest.mark.parametrize(
    "raw", [b"[]", b'{"jsonrpc":"2.0","id":4,"id":4,"result":{}}', b"[" * 2000 + b"]" * 2000]
)
def test_malformed_rpc_envelopes_refuse_without_resend(wire, raw):
    wire.transport.initialize()
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, content=raw)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        wire.transport.client = client
        with pytest.raises(DecisionReferenceError, match="No verified read result"):
            wire.transport.read("get_context", project_id=PROJECT)
    assert len(sent) == 1
