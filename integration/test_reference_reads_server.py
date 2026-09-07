import asyncio
import json

import httpx
import pytest
from mcp_server import decision_delivery as delivery
from mcp_server import generation_responses as generation
from mcp_server.read_arguments import ContextArguments, DecisionArguments, parse_read_arguments
from nauro.auth import ActiveCredentials
from nauro.mcp.decision_reference import reference_server
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.reference_reads import READ_SPECS, read_schema
from tests.conftest import TEST_PROJECT_ID
from tests.test_decision_reference import ORIGIN, action, prepare, probe, signed_transport
from tests.test_judgment_planning import USER_ID, _table

__all__ = ["probe", "signed_transport"]


def test_forward_existing_verified_generation_responses(probe, monkeypatch):
    saved = prepare(probe)
    committed = action(probe, saved, "submit")
    assert committed["status"] == "committed"
    expected = {
        "get_context": generation.get_context(USER_ID, TEST_PROJECT_ID, "L2"),
        "get_decision": generation.get_decision(USER_ID, TEST_PROJECT_ID, 1, "full"),
    }
    generation_id = json.loads(committed["execution"]["receipt_json"])["generation_id"]
    assert all(
        result.envelope["read_authority"]["generation_id"] == generation_id
        for result in expected.values()
    )
    monkeypatch.setattr(delivery, "run_pre_team_judgment", lambda *_: pytest.fail("Read executed"))
    reads = []

    def wire(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == probe.headers["Authorization"]
        if body["method"] == "tools/call" and body["params"]["name"] in READ_SPECS:
            name = body["params"]["name"]
            args = parse_read_arguments(name, body["params"]["arguments"])
            assert args.project_id == TEST_PROJECT_ID
            if isinstance(args, ContextArguments):
                response = generation.get_context(USER_ID, TEST_PROJECT_ID, args.level)
            else:
                assert isinstance(args, DecisionArguments)
                response = generation.get_decision(
                    USER_ID, TEST_PROJECT_ID, args.number, args.mode
                )
            reads.append(name)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": response.text}],
                        "isError": response.is_error,
                    },
                },
            )
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        if body["method"] == "tools/list":
            value = response.json()
            value["result"]["tools"].extend(
                {"name": name, "inputSchema": read_schema(name)} for name in READ_SPECS
            )
            return httpx.Response(200, json=value)
        return response

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        transport = DecisionReferenceTransport(
            ORIGIN + "/mcp",
            TEST_PROJECT_ID,
            USER_ID,
            client,
            lambda: ActiveCredentials(USER_ID, probe.headers["Authorization"][7:]),
        )
        transport.initialize()
        server = reference_server(transport)
        for name, arguments in [("get_context", {"level": "L2"}), ("get_decision", {"number": 1})]:
            tool = server._tool_manager.get_tool(name)
            result = asyncio.run(tool.run({"project_id": TEST_PROJECT_ID, **arguments}))
            assert result.isError is False
            assert result.content[0].text == expected[name].text
            assert generation_id in result.content[0].text
        _table().delete_item(Key={"pk": f"PROJECT#{TEST_PROJECT_ID}", "sk": f"MEMBER#{USER_ID}"})
        refused = transport.read("get_context", project_id=TEST_PROJECT_ID)
        assert refused["isError"] is True
        assert (
            refused["content"][0]["text"]
            == "Error: The current authorized generation is unavailable."
        )
        assert generation_id not in refused["content"][0]["text"]
    assert reads == ["get_context", "get_decision", "get_context"]
