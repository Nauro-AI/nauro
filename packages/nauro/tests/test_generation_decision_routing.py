from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from nauro_core.operations.commit_plan import canonical_judgment_payload_bytes
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.generation_decision import decision_session
from nauro.mcp.stdio_server import mcp
from nauro.store.config import save_config
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import register_project_v2
from nauro.store.submission_records import SubmissionScope
from nauro.sync import generation_decision as routing
from nauro.sync.decision_reference_contract import reference_schema
from nauro.sync.generation_credentials import AccountRecord
from tests.test_judgment_submission import USER, _payload
from tests.test_judgment_transport import _encoded_receipt, _receipt


class Authority:
    def __init__(self, project):
        self.project, self.records, self.calls = project, {}, []
        self.counter, self.commits = 4, 0
        self.loss = None
        self.rpc_methods = []

    def prepare(self, args):
        payload = json.loads(_payload())
        payload["base_decision_counter"] = self.counter
        payload["content"].update({k: v for k, v in args.items() if k in payload["content"]})
        raw = canonical_judgment_payload_bytes(payload)
        op = "decision-request:01K" + str(len(self.records) + 1).zfill(23)
        row = {
            "version": 1,
            "request": {
                "project_id": self.project,
                "actor_id": USER,
                "operation_id": op,
                "created_at": "2026-09-05T00:00:00.000000Z",
                "payload_digest": hashlib.sha256(raw).hexdigest(),
                "payload_json": raw.decode(),
            },
            "effective_draft": json.loads(raw),
            "admission": "never_admitted",
            "admitted_at": None,
            "deadline": None,
            "status": "prepared",
            "unresolved": False,
            "execution": None,
        }
        self.records[op] = row
        return row

    def call(self, args):
        self.calls.append(copy.deepcopy(args))
        mode = args.get("request_mode", "prepare")
        if mode == "prepare":
            result = self.prepare(args)
        elif mode == "discover":
            result = {"version": 1, "requests": list(self.records.values()), "next_after": None}
        else:
            result = self.records[args["operation_id"]]
            assert args["payload_digest"] == result["request"]["payload_digest"]
            if mode == "submit" and result["status"] == "prepared":
                if result["effective_draft"]["base_decision_counter"] != self.counter:
                    result["status"] = "stale"
                else:
                    self.counter += 1
                    self.commits += 1
                    scope = SubmissionScope(
                        project_id=self.project,
                        user_id=USER,
                        operation_kind="judgment_commit",
                        operation_id=args["operation_id"],
                    )
                    raw_receipt = _encoded_receipt(_receipt(SimpleNamespace(scope=scope)))
                    result.update(
                        admission="admitted",
                        admitted_at="2026-09-05T00:00:00.000000Z",
                        deadline="2026-09-06T00:00:00.000000Z",
                        status="committed",
                        execution={
                            "version": 1,
                            "scope": scope.model_dump(),
                            "payload_digest": args["payload_digest"],
                            "status": "committed",
                            "receipt_json": raw_receipt,
                        },
                    )
        if mode == self.loss:
            self.loss = None
            raise httpx.ReadError("response lost")
        return copy.deepcopy(result)

    def wire(self, request):
        body = json.loads(request.content)
        self.rpc_methods.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "propose_decision", "inputSchema": reference_schema()}]}
        else:
            value = self.call(body["params"]["arguments"])
            result = {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "isError": value.get("status") in {"stale", "pending"},
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def route(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unapproved path")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("NAURO_HOME", str(home))
    for name in (
        "NAURO_API_URL",
        "NAURO_AUTH0_DOMAIN",
        "NAURO_AUTH0_CLIENT_ID",
        "NAURO_AUTH0_AUDIENCE",
    ):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    project, store = register_project_v2(
        "Routing", [repo], mode="cloud", server_url="https://api.example"
    )
    (store / ".replica").mkdir(parents=True)
    marker = store / ".replica" / "authority.json"
    marker.write_bytes(
        GenerationAuthorityMarker(
            schema_version=1, authority="generation", project_id=project, store_format_version=1
        ).canonical_bytes()
    )
    save_config(
        {
            "api_url": "https://api.example",
            "auth0_domain": "issuer.example",
            "auth0_client_id": "client",
            "auth0_audience": "https://api.example/mcp",
        }
    )
    connection, _ = routing.select_decision_connection()
    account = connection.store()
    with account.locked():
        account.write(
            AccountRecord(
                revision="initial",
                binding=account.binding,
                state="active",
                user_id=USER,
                subject="owner",
                access_token="synthetic",
                refresh_token="synthetic-refresh",
                expires_at=int(time.time()) + 600,
            )
        )
    authority = Authority(project)
    decision_session.close()
    monkeypatch.setattr(
        routing,
        "reference_client",
        lambda: httpx.Client(transport=httpx.MockTransport(authority.wire)),
    )
    for name in ("tool_propose_decision", "tool_flag_question", "tool_update_state"):
        monkeypatch.setattr("nauro.mcp.stdio_server." + name, forbidden)
    monkeypatch.setattr("nauro.cli.autogen._resolve_adapter", forbidden)
    monkeypatch.setattr("nauro.sync.hooks.pull_before_session", forbidden)
    before = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}
    decision_session.close()
    yield SimpleNamespace(
        project=project,
        store=store,
        repo=repo,
        home=home,
        marker=marker,
        account=account,
        authority=authority,
        before=before,
    )
    decision_session.close()
    assert {
        str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()
    } == before


def cli(*args):
    return CliRunner().invoke(app, ["propose-decision", *args])


def tool(name="propose_decision", **args):
    return asyncio.run(mcp._tool_manager.get_tool(name).run(args))


def value(result):
    return json.loads(result.content[0].text)


def reference(row):
    return {key: row["request"][key] for key in ("operation_id", "payload_digest")}


def test_ordinary_cli_to_full_stdio_lost_response_recovery(route):
    route.authority.loss = "prepare"
    assert cli("Retain exact intent", "--title", "Draft").exit_code == 1
    draft = value(tool(request_mode="discover"))["requests"][0]
    assert draft["admission"] == "never_admitted"
    assert route.authority.commits == 0
    selected = reference(draft)
    route.authority.loss = "submit"
    submitted = cli(
        "--request-mode",
        "submit",
        "--operation-id",
        selected["operation_id"],
        "--payload-digest",
        selected["payload_digest"],
    )
    assert submitted.exit_code == 1
    discovered = value(tool(request_mode="discover"))["requests"][0]
    recovered = tool(request_mode="recover", **reference(discovered))
    assert recovered.isError is False
    assert value(recovered)["execution"]["receipt_json"] == discovered["execution"]["receipt_json"]
    assert route.authority.commits == 1
    repeated = tool(request_mode="submit", **selected)
    assert (
        value(repeated)["execution"]["receipt_json"]
        == value(recovered)["execution"]["receipt_json"]
    )
    assert route.authority.commits == 1
    assert [r.get("request_mode", "prepare") for r in route.authority.calls] == [
        "prepare",
        "discover",
        "submit",
        "discover",
        "recover",
        "submit",
    ]


def test_same_base_stale_requires_fresh_reference(route):
    first = value(tool(rationale="First", title="First"))
    second = value(tool(rationale="Second", title="Second"))
    assert tool(request_mode="submit", **reference(first)).isError is False
    stale = tool(request_mode="submit", **reference(second))
    assert stale.isError is True
    assert value(stale)["status"] == "stale"
    assert value(tool(request_mode="recover", **reference(second)))["status"] == "stale"
    fresh = value(tool(rationale="Second", title="Second"))
    assert reference(fresh) != reference(second)
    assert tool(request_mode="submit", **reference(fresh)).isError is False
    assert route.authority.commits == 2


@pytest.mark.parametrize(
    "extra",
    [{"title": ""}, {"rationale": None}, {"operation": "add"}, {"base_generation_id": "new"}],
)
def test_raw_reference_arguments_cannot_change_content(route, extra):
    draft = value(tool(rationale="Draft"))
    before = len(route.authority.calls)
    with pytest.raises(ToolError):
        tool(request_mode="submit", **reference(draft), **extra)
    assert len(route.authority.calls) == before
    assert route.authority.commits == 0


@pytest.mark.parametrize(
    "name,args",
    [("flag_question", {"question": "Question"}), ("update_state", {"delta": "Change"})],
)
def test_other_stdio_writes_refuse_before_local_adapter(route, name, args):
    with pytest.raises(ToolError, match="not supported"):
        tool(name, **args)
    assert route.authority.calls == []


def test_full_inventory_cwd_and_cli_project_selection(route, tmp_path, monkeypatch):
    assert len(mcp._tool_manager.list_tools()) == 10
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = tool(cwd=str(route.repo), rationale="With cwd")
    assert value(result)["request"]["project_id"] == route.project
    result = cli("--project", "Routing", "--request-mode", "discover")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["requests"][0]["request"]["project_id"] == route.project


def test_missing_credentials_never_fall_back(route):
    with route.account.locked():
        route.account.write(route.account.empty("logged_out"))
    assert cli("Draft").exit_code == 1
    with pytest.raises(ToolError):
        tool(rationale="Draft")
    assert route.authority.calls == []


def test_unreferenced_repetitions_only_create_inert_drafts(route):
    for _ in range(2):
        assert cli("Identical", "--title", "Same").exit_code == 0
    assert len(route.authority.records) == 2
    assert route.authority.commits == 0
    assert {r["status"] for r in route.authority.records.values()} == {"prepared"}


def test_full_stdio_process_discovers_and_recovers_existing_receipt(route, tmp_path):
    import os
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    draft = value(tool(rationale="Before restart"))
    committed = value(tool(request_mode="submit", **reference(draft)))
    state = tmp_path / "authority.json"
    state.write_text(json.dumps(route.authority.records))
    source = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(p)
            for p in (
                source / "packages/nauro/src",
                source / "packages/nauro-core/src",
                source / "packages/nauro",
            )
        ),
    }
    script = """
import json, socket, sys
from pathlib import Path
import httpx
from tests.test_generation_decision_routing import Authority
from nauro.sync import generation_decision as routing
from nauro.mcp.stdio_server import run_stdio
import nauro.sync.hooks

def forbidden(*args, **kwargs):
    raise AssertionError("External connection or pull forbidden")
socket.socket.connect = forbidden
nauro.sync.hooks.pull_before_session = forbidden
authority = Authority(sys.argv[1])
authority.records = json.loads(Path(sys.argv[2]).read_text())
routing.reference_client = lambda: httpx.Client(transport=httpx.MockTransport(authority.wire))
run_stdio()
"""

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", script, route.project, str(state)],
            env=env,
            cwd=str(route.repo),
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert len(listing.tools) == 10
            schema = next(t.inputSchema for t in listing.tools if t.name == "propose_decision")
            assert "cwd" in schema["properties"]
            assert "request_mode" in schema["properties"]
            found = await session.call_tool("propose_decision", {"request_mode": "discover"})
            assert found.isError is False
            recovered = await session.call_tool(
                "propose_decision",
                {"request_mode": "recover", **reference(value(found)["requests"][0])},
            )
            assert recovered.isError is False
            assert (
                value(recovered)["execution"]["receipt_json"]
                == committed["execution"]["receipt_json"]
            )
            invalid = await session.call_tool(
                "propose_decision", {"request_mode": "submit", **reference(draft), "title": ""}
            )
            assert invalid.isError is True
            refused = await session.call_tool("update_state", {"delta": "Forbidden"})
            assert refused.isError is True

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["update", "supersede"])
def test_existing_decision_prepare_omits_unsupplied_fields(route, operation):
    result = tool(
        rationale="Approved change",
        operation=operation,
        affected_decision_id="005-durable-identity",
    )
    assert value(result)["status"] == "prepared"
    assert route.authority.calls == [
        {
            "project_id": route.project,
            "request_mode": "prepare",
            "rationale": "Approved change",
            "operation": operation,
            "affected_decision_id": "005-durable-identity",
        }
    ]


def test_explicit_cli_project_overrides_another_repository(route, tmp_path, monkeypatch):
    from nauro.store.repo_config import save_repo_config

    other = tmp_path / "other"
    other.mkdir()
    pid, store = register_project_v2("Other", [other], mode="local")
    store.mkdir(parents=True, exist_ok=True)
    save_repo_config(other, {"mode": "local", "id": pid, "name": "Other"})
    monkeypatch.chdir(other)
    result = cli("--project", "Routing", "--request-mode", "discover")
    assert result.exit_code == 0, result.output
    assert route.authority.calls == [{"project_id": route.project, "request_mode": "discover"}]


def test_authority_change_during_negotiation_refuses_before_tool_call(route, monkeypatch):
    raw = route.marker.read_bytes()

    def wire(request):
        response = route.authority.wire(request)
        if json.loads(request.content)["method"] == "tools/list":
            route.marker.write_bytes(b"corrupt")
        return response

    decision_session.close()
    monkeypatch.setattr(
        routing, "reference_client", lambda: httpx.Client(transport=httpx.MockTransport(wire))
    )
    try:
        assert cli("Draft").exit_code == 1
        assert route.authority.calls == []
    finally:
        route.marker.write_bytes(raw)


def test_logout_after_commit_rejects_response_and_allows_later_lookup(route, monkeypatch):
    draft = value(tool(rationale="Before logout"))
    active = route.account.read()

    def wire(request):
        response = route.authority.wire(request)
        args = json.loads(request.content).get("params", {}).get("arguments", {})
        if args.get("request_mode") == "submit":
            with route.account.locked():
                route.account.write(route.account.empty("logged_out"))
        return response

    decision_session.close()
    monkeypatch.setattr(
        routing, "reference_client", lambda: httpx.Client(transport=httpx.MockTransport(wire))
    )
    with pytest.raises(ToolError):
        tool(request_mode="submit", **reference(draft))
    assert route.authority.commits == 1
    with route.account.locked():
        route.account.write(active)
    recovered = tool(request_mode="recover", **reference(draft))
    assert recovered.isError is False
    assert value(recovered)["status"] == "committed"
    assert [r["request_mode"] for r in route.authority.calls] == ["prepare", "submit", "recover"]


def test_pending_recovery_is_an_error_without_resubmission(route):
    draft = value(tool(rationale="Pending"))
    row = route.authority.records[draft["request"]["operation_id"]]
    scope = SubmissionScope(
        project_id=route.project,
        user_id=USER,
        operation_kind="judgment_commit",
        operation_id=draft["request"]["operation_id"],
    )
    row.update(
        admission="admitted",
        admitted_at="2026-09-05T00:00:00.000000Z",
        deadline="2026-09-06T00:00:00.000000Z",
        status="pending",
        unresolved=True,
        execution={
            "version": 1,
            "scope": scope.model_dump(),
            "payload_digest": row["request"]["payload_digest"],
            "status": "pending",
            "receipt_json": None,
        },
    )
    assert tool(request_mode="recover", **reference(draft)).isError is True
    args = reference(draft)
    result = cli(
        "--request-mode",
        "recover",
        "--operation-id",
        args["operation_id"],
        "--payload-digest",
        args["payload_digest"],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["unresolved"] is True
    assert [r.get("request_mode", "prepare") for r in route.authority.calls] == [
        "prepare",
        "recover",
        "recover",
    ]


def test_stdio_reuses_negotiation_but_cli_owns_separate_sessions(route):
    tool(request_mode="discover")
    tool(request_mode="discover")
    assert route.authority.rpc_methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    assert cli("--request-mode", "discover").exit_code == 0
    assert route.authority.rpc_methods[-4:] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert len(route.authority.rpc_methods) == 9
    previous = decision_session._client
    decision_session.close()
    assert previous.is_closed is True
    tool(request_mode="discover")
    assert len(route.authority.rpc_methods) == 13
