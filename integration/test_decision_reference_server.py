"""Installed client decision binding against the signed isolated application."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import decision_delivery as delivery
from mcp_server.decision_probe import create_decision_probe
from mcp_server.generations import read_generation_pointer
from nauro.auth import ActiveCredentials
from nauro.mcp.decision_reference import bind_decision_reference
from nauro.sync.decision_reference import DecisionReferenceTransport
from tests.conftest import TEST_PROJECT_ID
from tests.test_decision_reference import ORIGIN, probe, signed_transport
from tests.test_judgment_planning import USER_ID
from tests.test_judgment_transport import _payload

__all__ = ["probe", "signed_transport"]


@pytest.fixture
def installed(probe):
    from nauro.mcp.stdio_server import mcp

    original = mcp._tool_manager.get_tool("propose_decision")

    def credentials():
        return ActiveCredentials(USER_ID, probe.headers["Authorization"][7:])

    transport = DecisionReferenceTransport(
        ORIGIN + "/mcp", TEST_PROJECT_ID, USER_ID, probe, credentials
    )
    bind_decision_reference(mcp, transport)
    tool = mcp._tool_manager.get_tool("propose_decision")

    def invoke(**arguments):
        response = asyncio.run(tool.run({"project_id": TEST_PROJECT_ID, **arguments}))
        return json.loads(response.content[0].text)

    yield invoke, transport
    mcp._tool_manager._tools["propose_decision"] = original


def prepare(invoke, **changes):
    return invoke(
        **{
            k: v
            for k, v in {**json.loads(_payload())["content"], **changes}.items()
            if v is not None
        }
    )


def action(invoke, saved, mode):
    return invoke(
        request_mode=mode,
        operation_id=saved["request"]["operation_id"],
        payload_digest=saved["request"]["payload_digest"],
    )


def test_installed_tool_prepares_commits_and_recovers(installed):
    invoke, _ = installed
    saved = prepare(invoke)
    assert saved["admission"] == "never_admitted"
    assert action(invoke, saved, "recover") == saved
    committed = action(invoke, saved, "submit")
    assert committed["status"] == "committed"
    assert action(invoke, saved, "submit") == committed
    assert action(invoke, saved, "recover") == committed
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 1


@pytest.mark.parametrize("lost_mode", ["prepare", "submit"])
def test_installed_client_discovers_after_lost_response(installed, probe, monkeypatch, lost_mode):
    invoke, transport = installed
    saved = prepare(invoke) if lost_mode == "submit" else None
    observed = []

    def wire(request):
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        body = json.loads(request.content)
        if body["method"] == "tools/call":
            observed.append(json.loads(response.json()["result"]["content"][0]["text"]))
            raise httpx.ReadError("Controlled lost response", request=request)
        return response

    transport.client = httpx.Client(transport=httpx.MockTransport(wire))
    with pytest.raises(ToolError, match="No verified result"):
        if saved is None:
            prepare(invoke)
        else:
            action(invoke, saved, "submit")
    assert len(observed) == 1
    transport.client.close()
    restarted = DecisionReferenceTransport(
        ORIGIN + "/mcp", TEST_PROJECT_ID, USER_ID, probe, transport.credentials
    )
    monkeypatch.setattr(
        delivery, "run_pre_team_judgment", lambda *_: pytest.fail("Recovery dispatched")
    )
    found = restarted.propose_decision(project_id=TEST_PROJECT_ID, request_mode="discover")
    assert found["next_after"] is None
    assert found["requests"] == observed
    recovered = action(
        lambda **kw: restarted.propose_decision(project_id=TEST_PROJECT_ID, **kw),
        found["requests"][0],
        "recover",
    )
    assert recovered == observed[0]
    assert recovered["status"] == ("prepared" if lost_mode == "prepare" else "committed")
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == (
        0 if lost_mode == "prepare" else 1
    )


def test_installed_client_stale_requires_fresh_request(installed):
    invoke, _ = installed
    first = prepare(invoke, title="First client decision")
    second = prepare(invoke, title="Second client decision")
    assert action(invoke, first, "submit")["status"] == "committed"
    assert action(invoke, second, "submit")["status"] == "stale"
    fresh = prepare(invoke, title="Second client decision")
    assert fresh["request"]["operation_id"] != second["request"]["operation_id"]
    assert fresh["effective_draft"]["base_decision_counter"] == 1
    assert action(invoke, fresh, "submit")["status"] == "committed"
    with pytest.raises(ToolError, match="Invalid saved request"):
        action(invoke, second, "retry")
    assert action(invoke, second, "recover")["status"] == "stale"


def test_installed_client_retry_reservation_fences_delayed_original(installed, monkeypatch):
    invoke, transport = installed
    saved = prepare(invoke)
    entered, release = Event(), Event()
    run = delivery.dispatch
    calls = []

    def delayed(request):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return run(request)

    monkeypatch.setattr(delivery, "dispatch", delayed)
    from mcp_server import judgment_execution

    original_stage = judgment_execution.publish_judgment

    def stage(*args, **kwargs):
        release.set()
        assert original_done.wait(10)
        return original_stage(*args, **kwargs)

    original_done = Event()
    monkeypatch.setattr(judgment_execution, "publish_judgment", stage)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(action, invoke, saved, "submit")
        future.add_done_callback(lambda _: original_done.set())
        assert entered.wait(10)
        try:
            with TestClient(create_decision_probe(), base_url=ORIGIN) as other:
                second = DecisionReferenceTransport(
                    ORIGIN + "/mcp", TEST_PROJECT_ID, USER_ID, other, transport.credentials
                )
                retry = action(
                    lambda **kw: second.propose_decision(project_id=TEST_PROJECT_ID, **kw),
                    saved,
                    "retry",
                )
                assert retry["status"] == "committed"
                assert future.result(timeout=10)["status"] == "pending"
        finally:
            release.set()
    assert (
        action(invoke, saved, "recover")["execution"]["receipt_json"]
        == retry["execution"]["receipt_json"]
    )
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 1


@pytest.mark.parametrize(
    "change",
    [
        {"rationale": "Altered"},
        {"base_generation_id": "new"},
        {"title": None},
        {"caller_capability": "durable"},
    ],
)
def test_installed_tool_refuses_replacement_fields(installed, change):
    invoke, _ = installed
    saved = prepare(invoke)
    with pytest.raises(ToolError, match="Replacement content"):
        invoke(
            request_mode="submit",
            operation_id=saved["request"]["operation_id"],
            payload_digest=saved["request"]["payload_digest"],
            **change,
        )
    assert action(invoke, saved, "recover")["admission"] == "never_admitted"
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 0


def test_installed_client_revoked_owner_cannot_submit_or_recover(installed):
    from tests.test_judgment_planning import _table

    invoke, _ = installed
    saved = prepare(invoke)
    _table().delete_item(Key={"pk": f"PROJECT#{TEST_PROJECT_ID}", "sk": f"MEMBER#{USER_ID}"})
    for mode in ("submit", "recover"):
        with pytest.raises(ToolError, match="No verified result"):
            action(invoke, saved, mode)
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 0


@pytest.mark.parametrize("field", ["payload_json", "effective_draft", "deadline", "receipt_json"])
def test_installed_client_rejects_altered_evidence(installed, probe, field):
    invoke, transport = installed
    saved = prepare(invoke)
    committed = action(invoke, saved, "submit")

    def wire(request):
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        envelope = response.json()
        value = json.loads(envelope["result"]["content"][0]["text"])
        if field == "payload_json":
            value["request"][field] += " "
        elif field == "effective_draft":
            value[field]["base_decision_counter"] = 999
        elif field == "deadline":
            value[field] = value["admitted_at"]
        else:
            value["execution"][field] += " "
        envelope["result"]["content"][0]["text"] = json.dumps(value)
        return httpx.Response(200, json=envelope)

    with httpx.Client(transport=httpx.MockTransport(wire)) as altered:
        transport.client = altered
        with pytest.raises(ToolError, match="Invalid saved request or receipt"):
            action(invoke, saved, "recover")
    transport.client = probe
    assert action(invoke, saved, "recover") == committed


def test_installed_reference_schema_matches_host_and_tool_count(installed):
    from mcp_server.decision_probe import tool_spec
    from nauro.mcp.stdio_server import mcp
    from nauro.sync.decision_reference_contract import reference_schema

    assert mcp._tool_manager.get_tool("propose_decision").parameters == tool_spec()["inputSchema"]
    assert reference_schema() == tool_spec()["inputSchema"]
    assert len(mcp._tool_manager.list_tools()) == 10


def test_installed_client_duplicate_drafts_stay_inert(installed):
    invoke, _ = installed
    first, second = prepare(invoke), prepare(invoke)
    assert first["request"]["operation_id"] != second["request"]["operation_id"]
    assert first["request"]["payload_json"] == second["request"]["payload_json"]
    assert action(invoke, first, "recover")["admission"] == "never_admitted"
    assert action(invoke, second, "recover")["admission"] == "never_admitted"
    page = invoke(request_mode="discover")
    next_page = invoke(request_mode="discover", after=page["next_after"])
    assert {
        page["requests"][0]["request"]["operation_id"],
        next_page["requests"][0]["request"]["operation_id"],
    } == {first["request"]["operation_id"], second["request"]["operation_id"]}
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 0


def test_hosted_driver_executes_installed_tool_only_against_local_app(
    installed, probe, signed_transport, tmp_path, monkeypatch
):
    import runpy
    import sys
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace

    import jwt
    from mcp_server import auth

    endpoint = "https://decision-probe.nauro.ai"
    monkeypatch.setattr(probe.app.state, "origin", endpoint)
    monkeypatch.setattr(auth, "AUTH0_AUDIENCE", endpoint + "/mcp")
    token = jwt.encode(
        {**signed_transport.claims, "aud": endpoint + "/mcp"},
        signed_transport.key,
        algorithm="RS256",
    )
    probe.headers["Authorization"] = "Bearer " + token
    driver = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "controlled_decision_reference.py")
    )

    @contextmanager
    def local_client(**kwargs):
        yield probe

    driver["main"].__globals__["httpx"] = SimpleNamespace(Client=local_client)
    manifest, request, evidence, credentials = [
        tmp_path / name
        for name in ("manifest.json", "request.json", "evidence.jsonl", "credentials.json")
    ]
    manifest.write_text(
        json.dumps(
            {
                "endpoint": "https://decision-probe.nauro.ai/mcp",
                "project_id": TEST_PROJECT_ID,
                "actor_id": USER_ID,
                "isolated": True,
            }
        )
    )
    request.write_text(
        json.dumps(
            {
                "project_id": TEST_PROJECT_ID,
                "rationale": "Synthetic local driver check",
                "title": "Local driver decision",
            }
        )
    )
    credentials.write_text(
        json.dumps({"user_id": USER_ID, "access_token": probe.headers["Authorization"][7:]})
    )
    credentials.chmod(0o600)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "driver",
            "--manifest",
            str(manifest),
            "--request",
            str(request),
            "--evidence",
            str(evidence),
            "--credentials",
            str(credentials),
            "--execute",
        ],
    )
    assert driver["main"]() == 0
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert [r["status"] for r in records] == ["started_outcome_unknown", "verified"]
    assert records[1]["result"]["admission"] == "never_admitted"
    assert "access_token" not in evidence.read_text()
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 0
