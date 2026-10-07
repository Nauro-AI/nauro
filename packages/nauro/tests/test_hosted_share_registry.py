"""Exact share registry negotiation preserves existing connection boundaries."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nauro_core.mcp_tools import SHARE_CONTEXT

from nauro.sync.decision_reference_contract import DecisionReferenceError
from nauro.sync.reference_reads import negotiate_registry
from tests.test_hosted_question_registry import assert_refused, question_schema
from tests.test_hosted_stack_registry import registry as stack_registry
from tests.test_hosted_stack_registry import stack_schema, tool
from tests.test_hosted_state_registry import state_schema
from tests.test_reference_reads import PROJECT, wire

__all__ = ["wire"]
FIXTURE = Path(__file__).parent / "fixtures" / "hosted_share_schema.json"
DIGEST = "a" * 64
OPERATION = "share-request:01M44W51SX9R69FPV6B07A2DS0"
REFERENCE = {"operation_id": OPERATION, "payload_digest": DIGEST}
BRIEF = {"slug": "handoff-1", "content": "# Brief\n", "pointer_kind": "brief", "summary": "Handoff"}
REMOVE = object()


def share_schema():
    return json.loads(FIXTURE.read_text())


def registry(questions, state, stack, share):
    value = stack_registry(questions, state, stack)
    if share:
        tool(value, "share_context")["inputSchema"] = share_schema()
    return value


def without(name):
    return {key: value for key, value in BRIEF.items() if key != name}


def test_schema_matches_reviewed_fixture():
    from nauro.sync.hosted_share_schema import share_schema as implementation

    core = copy.deepcopy(SHARE_CONTEXT["input_schema"])
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "bedc4f6dea4fb496ee63a6a710004e16c105b7d1b00a2eb70bdd4d7bd810d674"
    )
    assert implementation() == share_schema()
    assert json.dumps(implementation(), indent=2) + "\n" == FIXTURE.read_text()
    Draft202012Validator.check_schema(implementation())
    assert SHARE_CONTEXT["input_schema"] == core


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("state", [False, True])
@pytest.mark.parametrize("stack", [False, True])
@pytest.mark.parametrize("share", [False, True])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_transport_preserves_decisions_and_read_limits(
    wire, questions, state, stack, share, reversed_order
):
    value = registry(questions, state, stack, share)
    if reversed_order:
        value["tools"].reverse()
    wire.control["registry"] = value
    wire.transport.initialize()
    assert wire.transport._initialized is True
    assert wire.transport.reads_available is False
    assert negotiate_registry(value) is False
    page = {"version": 1, "requests": [], "next_after": None}
    wire.control["result"] = {
        "content": [{"type": "text", "text": json.dumps(page)}],
        "isError": False,
    }
    assert wire.transport.propose_decision(project_id=PROJECT, request_mode="discover") == page
    assert wire.calls[-1][0]["params"] == {
        "name": "propose_decision",
        "arguments": {"project_id": PROJECT, "request_mode": "discover"},
    }
    before = list(wire.calls)
    with pytest.raises(DecisionReferenceError, match="does not expose"):
        wire.transport.read("get_context", project_id=PROJECT)
    assert wire.calls == before


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("state", [False, True])
@pytest.mark.parametrize("stack", [False, True])
@pytest.mark.parametrize("share", [False, True])
@pytest.mark.parametrize(
    "change", ["other_schema", "duplicate", "missing", "rename", "pagination", "extra"]
)
def test_registry_remains_closed(wire, questions, state, stack, share, change):
    value = registry(questions, state, stack, share)
    if change == "other_schema":
        tool(value, "get_context")["inputSchema"] = {}
    elif change == "duplicate":
        value["tools"][-1] = value["tools"][0]
    elif change == "missing":
        value["tools"].pop()
    elif change == "rename":
        value["tools"][-1]["name"] = "unknown"
    elif change == "extra":
        value["tools"].append({"name": "unknown", "inputSchema": {}})
    else:
        value["nextCursor"] = None
    assert_refused(wire, value)


@pytest.mark.parametrize("name", ["flag_question", "update_state", "update_stack", "share_context"])
def test_coexisting_schemas_each_stay_exact(wire, name):
    value = registry(True, True, True, True)
    tool(value, name)["inputSchema"]["title"] = "changed"
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "name,schema",
    [
        ("update_state", share_schema),
        ("update_stack", share_schema),
        ("share_context", stack_schema),
        ("share_context", state_schema),
        ("share_context", question_schema),
    ],
)
def test_recognized_schemas_do_not_cross_tools(wire, name, schema):
    value = registry(True, True, True, True)
    tool(value, name)["inputSchema"] = schema()
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("properties", "slug", "description"), "Brief name."),
        (("properties", "content", "maxLength"), 51200),
        (("properties", "summary", "maxLength"), 300),
        (("properties", "pointer_kind", "enum"), ["brief"]),
        (
            ("properties", "operation_id", "description"),
            SHARE_CONTEXT["input_schema"]["properties"]["operation_id"]["description"],
        ),
        (("properties", "operation_id", "pattern"), "^share-request:[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
        (("properties", "project_id", "description"), "Project."),
        (("properties", "expected_revision"), {"type": "string"}),
        (("properties", "request_mode", "enum"), ["submit"]),
        (("properties", "payload_digest", "maxLength"), 65),
        (("properties", "after", "maxLength"), 257),
        (("required",), ["slug", "content", "pointer_kind", "summary"]),
        (("additionalProperties",), False),
        (("oneOf", 0, "required"), ["request_mode", "slug", "content", "pointer_kind"]),
        (
            ("oneOf", 1, "propertyNames", "enum"),
            ["project_id", "slug", "content", "pointer_kind", "summary", "operation_id"],
        ),
        (
            ("oneOf", 2, "properties", "operation_id", "pattern"),
            "^stack-request:[0-7][0-9A-HJKMNP-TV-Z]{25}$",
        ),
        (("oneOf", 3, "properties", "operation_id", "minLength"), 39),
        (("oneOf", 3, "properties", "request_mode", "const"), "recover"),
        (("oneOf", 4, "propertyNames", "enum"), ["project_id"]),
        (("oneOf", 5, "not", "anyOf"), []),
        (("oneOf", 5, "required"), ["content", "operation_id"]),
        (("oneOf", 5), REMOVE),
    ],
)
def test_nested_drift_refuses_before_call(wire, path, replacement):
    value = registry(True, True, True, True)
    target = tool(value, "share_context")["inputSchema"]
    for part in path[:-1]:
        target = target[part]
    if replacement is REMOVE:
        del target[path[-1]]
    else:
        target[path[-1]] = replacement
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "arguments",
    [
        {"request_mode": "submit", **BRIEF},
        {"request_mode": "submit", **BRIEF, "project_id": PROJECT},
        dict(BRIEF),
        {**BRIEF, "project_id": PROJECT},
        {**BRIEF, "pointer_kind": "resume"},
        {**BRIEF, "pointer_kind": "selection"},
        {"slug": "", "content": "", "pointer_kind": "brief", "summary": ""},
        {**BRIEF, "content": "x" * 51200},
        {**BRIEF, "content": "x" * 60000},
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE}
            for mode in ("recover", "retry")
        ],
        {
            "request_mode": "recover",
            "project_id": PROJECT,
            **REFERENCE,
            "operation_id": "share-request:7" + "Z" * 25,
        },
        {"request_mode": "discover", "project_id": PROJECT},
        {"request_mode": "discover", "project_id": PROJECT, "after": "cursor"},
        {**BRIEF, "operation_id": "op-1"},
        {**BRIEF, "operation_id": "op-1", "project_id": PROJECT},
        {**BRIEF, "operation_id": OPERATION},
        {**BRIEF, "operation_id": "op-1", "legacy_field": 1},
        {**BRIEF, "content": "x" * 60000, "operation_id": "op-1"},
    ],
)
def test_supported_payloads(arguments):
    assert list(Draft202012Validator(share_schema()).iter_errors(arguments)) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        *[without(name) for name in BRIEF],
        *[{"request_mode": "submit", **without(name)} for name in BRIEF],
        *[{**without(name), "operation_id": "op-1"} for name in BRIEF],
        *[
            {"request_mode": "submit", **BRIEF, **extra}
            for extra in (
                {"operation_id": OPERATION},
                REFERENCE,
                {"after": "c"},
                {"expected_revision": DIGEST},
            )
        ],
        *[
            {**BRIEF, **extra}
            for extra in (
                {"expected_revision": DIGEST},
                {"expected_revision": "absent"},
                {"payload_digest": DIGEST},
                {"unknown": 1},
                {"pointer_kind": "BRIEF"},
                {"slug": None},
                {"summary": 1},
            )
        ],
        *[
            {**BRIEF, "operation_id": "op-1", **extra}
            for extra in (
                {"request_mode": "submit"},
                {"request_mode": None},
                {"payload_digest": DIGEST},
                {"after": "c"},
                {"operation_id": None},
                {"pointer_kind": "note"},
            )
        ],
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE, "operation_id": operation}
            for mode, operation in (
                ("recover", "question-request:" + "0" * 26),
                ("recover", "state-request:" + "0" * 26),
                ("recover", "stack-request:" + "0" * 26),
                ("recover", "share-request:8" + "0" * 25),
                ("recover", "share-request:" + "I" * 26),
                ("recover", OPERATION.lower()),
                ("recover", OPERATION + "\n"),
                ("recover", "op-1"),
                ("retry", "op-1"),
            )
        ],
        {"request_mode": "recover", **REFERENCE},
        {"request_mode": "recover", "project_id": PROJECT, "operation_id": OPERATION},
        *[
            {
                "request_mode": "recover",
                "project_id": PROJECT,
                **REFERENCE,
                "payload_digest": digest,
            }
            for digest in ("A" * 64, DIGEST + "\n")
        ],
        {"request_mode": "recover", "project_id": PROJECT, **REFERENCE, "slug": "handoff-1"},
        {"request_mode": "retry", "project_id": PROJECT, **REFERENCE, "content": "# Brief\n"},
        {"request_mode": "retry", "project_id": PROJECT, **REFERENCE, **BRIEF},
        {"request_mode": "discover", "project_id": PROJECT, **REFERENCE},
        {"request_mode": "discover", "project_id": PROJECT, **BRIEF},
        {"request_mode": "discover", "project_id": PROJECT, "after": "a" * 257},
        {"request_mode": "discover"},
        {"request_mode": "unknown", **BRIEF},
    ],
)
def test_invalid_payloads(arguments):
    assert Draft202012Validator(share_schema()).is_valid(arguments) is False
