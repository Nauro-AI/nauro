"""Fail-closed reference negotiation and dormant production registration."""

import json

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.sync.decision_reference import DecisionReferenceError, DecisionReferenceTransport
from nauro.sync.decision_reference_contract import reference_schema

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
ACTOR = "01K" + "0" * 21 + "08"
REFERENCE = "decision-request:" + "01K" + "0" * 21 + "09"


@pytest.mark.parametrize(
    "change",
    [
        {"request_mode": "submit", "operation_id": "invented", "payload_digest": "0" * 64},
        {"request_mode": "prepare", "rationale": "draft", "operation_id": REFERENCE},
        {
            "request_mode": "recover",
            "operation_id": REFERENCE,
            "payload_digest": "0" * 64,
            "rationale": "changed",
        },
        {"request_mode": "discover", "operation_id": REFERENCE},
    ],
)
def test_invalid_intent_never_transmits(change):
    def wire(_):
        pytest.fail("Invalid intent reached the network")

    with httpx.Client(transport=httpx.MockTransport(wire)) as http:
        transport = DecisionReferenceTransport("https://probe.example/mcp", PROJECT, ACTOR, http)
        with pytest.raises(ValueError):
            transport.propose_decision(project_id=PROJECT, **change)


def test_original_single_call_schema_refuses_before_tool_execution():
    seen = []

    def wire(request):
        body = json.loads(request.content)
        seen.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {"protocolVersion": "2025-06-18"}
            if body["method"] == "initialize"
            else {
                "tools": [{"name": "propose_decision", "inputSchema": {"required": ["rationale"]}}]
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with httpx.Client(transport=httpx.MockTransport(wire)) as http:
        transport = DecisionReferenceTransport(
            "https://probe.example/mcp",
            PROJECT,
            ACTOR,
            http,
            lambda: ActiveCredentials(ACTOR, "synthetic"),
        )
        with pytest.raises(DecisionReferenceError, match="Unexpected isolated tool registry"):
            transport.propose_decision(project_id=PROJECT, rationale="draft")
    assert seen == ["initialize", "notifications/initialized", "tools/list"]


def test_wrong_active_actor_never_transmits():
    def wire(_):
        pytest.fail("Wrong actor reached the network")

    with httpx.Client(transport=httpx.MockTransport(wire)) as http:
        transport = DecisionReferenceTransport(
            "https://probe.example/mcp",
            PROJECT,
            ACTOR,
            http,
            lambda: ActiveCredentials(PROJECT, "synthetic"),
        )
        with pytest.raises(DecisionReferenceError, match="Active account differs"):
            transport.propose_decision(project_id=PROJECT, rationale="draft")


def test_http_failure_does_not_retry_or_follow_redirect():
    seen = []

    def wire(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example/mcp"})

    with httpx.Client(transport=httpx.MockTransport(wire), follow_redirects=True) as http:
        transport = DecisionReferenceTransport(
            "https://probe.example/mcp",
            PROJECT,
            ACTOR,
            http,
            lambda: ActiveCredentials(ACTOR, "synthetic"),
        )
        with pytest.raises(DecisionReferenceError, match="No verified result"):
            transport.propose_decision(project_id=PROJECT, rationale="draft")
    assert seen == ["https://probe.example/mcp"]


def test_normal_tool_adds_reference_modes_without_new_tools():
    from nauro.mcp.stdio_server import mcp

    tool = mcp._tool_manager.get_tool("propose_decision")
    assert "request_mode" in tool.parameters["properties"]
    assert "rationale" not in tool.parameters.get("required", [])
    assert tool.parameters["oneOf"][0]["required"] == ["rationale"]
    assert len(mcp._tool_manager.list_tools()) == 10
    assert reference_schema()["properties"]["request_mode"]["default"] == "prepare"


def test_hosted_driver_defaults_to_no_network(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path

    manifest = tmp_path / "manifest.json"
    request = tmp_path / "request.json"
    evidence = tmp_path / "evidence.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "endpoint": "https://decision-probe.nauro.ai/mcp",
                "project_id": PROJECT,
                "actor_id": ACTOR,
                "isolated": True,
            }
        )
    )
    request.write_text(json.dumps({"project_id": PROJECT, "request_mode": "discover"}))

    def no_client(*args, **kwargs):
        pytest.fail("Driver dry run opened an HTTP client")

    monkeypatch.setattr(httpx, "Client", no_client)
    driver = Path(__file__).resolve().parents[3] / "scripts" / "controlled_decision_reference.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(driver),
            "--manifest",
            str(manifest),
            "--request",
            str(request),
            "--evidence",
            str(evidence),
        ],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(driver), run_name="__main__")
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["network_called"] is False
    assert evidence.exists() is False
