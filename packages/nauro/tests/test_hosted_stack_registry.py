"""Exact stack registry negotiation preserves existing connection boundaries."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nauro_core.mcp_tools import UPDATE_STACK

from nauro.sync.decision_reference_contract import DecisionReferenceError
from nauro.sync.reference_reads import negotiate_registry
from tests.test_hosted_question_registry import assert_refused, question_schema
from tests.test_hosted_state_registry import registry as state_registry
from tests.test_hosted_state_registry import state_schema
from tests.test_reference_reads import PROJECT, wire

__all__ = ["wire"]
FIXTURE = Path(__file__).parent / "fixtures" / "hosted_stack_schema.json"
REVISION = "a" * 64
OPERATION = "stack-request:01M44W51SX9R69FPV6B07A2DS0"
REFERENCE = {"operation_id": OPERATION, "payload_digest": REVISION}
REMOVE = object()


def stack_schema():
    return json.loads(FIXTURE.read_text())


def tool(value, name):
    return next(t for t in value["tools"] if t["name"] == name)


def registry(questions, state, stack):
    value = state_registry(questions, state)
    if stack:
        tool(value, "update_stack")["inputSchema"] = stack_schema()
    return value


def test_schema_matches_reviewed_fixture():
    from nauro.sync.hosted_stack_schema import stack_schema as implementation

    core = copy.deepcopy(UPDATE_STACK["input_schema"])
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "fd5027265cf34a9dd3852e2779e4a52938862fd638fe86c4c047a873ed206d71"
    )
    assert implementation() == stack_schema()
    assert json.dumps(implementation(), indent=2) + "\n" == FIXTURE.read_text()
    Draft202012Validator.check_schema(implementation())
    assert UPDATE_STACK["input_schema"] == core


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("state", [False, True])
@pytest.mark.parametrize("stack", [False, True])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_transport_preserves_decisions_and_read_limits(
    wire, questions, state, stack, reversed_order
):
    value = registry(questions, state, stack)
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
@pytest.mark.parametrize(
    "change", ["other_schema", "duplicate", "missing", "rename", "pagination", "extra"]
)
def test_registry_remains_closed(wire, questions, state, stack, change):
    value = registry(questions, state, stack)
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


@pytest.mark.parametrize("name", ["flag_question", "update_state", "update_stack"])
def test_coexisting_schemas_each_stay_exact(wire, name):
    value = registry(True, True, True)
    tool(value, name)["inputSchema"]["title"] = "changed"
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "name,schema",
    [
        ("update_state", stack_schema),
        ("update_stack", state_schema),
        ("update_stack", question_schema),
    ],
)
def test_recognized_schemas_do_not_cross_tools(wire, name, schema):
    value = registry(True, True, True)
    tool(value, name)["inputSchema"] = schema()
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("properties", "expected_revision", "anyOf", 1, "maxLength"), 65),
        (("properties", "expected_revision", "description"), "Use latest revision"),
        (("properties", "operation_id", "description"), "Caller identifier"),
        (("properties", "operation_id", "pattern"), ".*"),
        (("properties", "request_mode", "enum"), ["submit"]),
        (("properties", "payload_digest", "maxLength"), 65),
        (("properties", "after", "maxLength"), 257),
        (("properties", "content", "maxLength"), 40000),
        (("oneOf", 0, "required"), ["content"]),
        (
            ("oneOf", 1, "propertyNames", "enum"),
            ["project_id", "content", "expected_revision", "operation_id"],
        ),
        (("oneOf", 2, "properties", "operation_id", "pattern"), ".*"),
        (("oneOf", 3, "properties", "operation_id", "minLength"), 39),
        (("oneOf", 3, "properties", "request_mode", "const"), "recover"),
        (("oneOf", 4, "propertyNames", "enum"), ["project_id"]),
        (("oneOf", 5, "not", "anyOf"), []),
        (("oneOf", 5, "required"), ["content"]),
        (("oneOf", 5), REMOVE),
    ],
)
def test_nested_drift_refuses_before_call(wire, path, replacement):
    value = registry(True, True, True)
    target = tool(value, "update_stack")["inputSchema"]
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
        {
            "request_mode": "submit",
            "content": "# s",
            "expected_revision": REVISION,
            "project_id": PROJECT,
        },
        {"request_mode": "submit", "content": "", "expected_revision": "absent"},
        {"content": "# s", "expected_revision": REVISION},
        {"content": "# s", "expected_revision": "absent", "project_id": PROJECT},
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE}
            for mode in ("recover", "retry")
        ],
        {
            "request_mode": "recover",
            "project_id": PROJECT,
            **REFERENCE,
            "operation_id": "stack-request:7" + "Z" * 25,
        },
        {"request_mode": "discover", "project_id": PROJECT},
        {"request_mode": "discover", "project_id": PROJECT, "after": "cursor"},
        {"content": "# s", "operation_id": "op-1"},
        {
            "content": "# s",
            "operation_id": "op-1",
            "expected_revision": REVISION,
            "project_id": PROJECT,
        },
        {"content": "# s", "operation_id": "op-1", "expected_revision": "absent"},
        {"content": "# s", "operation_id": OPERATION},
        {"content": "# s", "operation_id": "op-1", "legacy_field": 1},
        {"content": "x" * 40000, "operation_id": "op-1"},
    ],
)
def test_supported_payloads(arguments):
    assert list(Draft202012Validator(stack_schema()).iter_errors(arguments)) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"content": "# s"},
        {"content": "# s", "project_id": PROJECT},
        {"request_mode": "submit", "content": "# s", "expected_revision": REVISION, **REFERENCE},
        {
            "request_mode": "submit",
            "content": "# s",
            "expected_revision": REVISION,
            "operation_id": OPERATION,
        },
        *[
            {"request_mode": "submit", "content": "# s", **revision}
            for revision in ({}, {"expected_revision": None}, {"expected_revision": "A" * 64})
        ],
        *[
            {"content": "# s", "expected_revision": revision}
            for revision in (
                None,
                "",
                "ABSENT",
                "absent\n",
                "a" * 63,
                "a" * 65,
                "A" * 64,
                REVISION + "\n",
            )
        ],
        {"content": "# s", "expected_revision": REVISION, "unknown": 1},
        {"request_mode": "unknown", "content": "# s", "expected_revision": REVISION},
        *[
            {"content": "# s", "operation_id": "op-1", **extra}
            for extra in (
                {"request_mode": "submit"},
                {"request_mode": None},
                {"payload_digest": REVISION},
                {"after": "c"},
                {"expected_revision": "A" * 64},
                {"expected_revision": None},
            )
        ],
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE, "operation_id": operation}
            for mode, operation in (
                ("recover", "question-request:" + "0" * 26),
                ("recover", "state-request:" + "0" * 26),
                ("recover", "stack-request:8" + "0" * 25),
                ("recover", "stack-request:" + "I" * 26),
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
            for digest in ("A" * 64, REVISION + "\n")
        ],
        {"request_mode": "recover", "project_id": PROJECT, **REFERENCE, "content": "# s"},
        {
            "request_mode": "retry",
            "project_id": PROJECT,
            **REFERENCE,
            "expected_revision": REVISION,
        },
        {"request_mode": "discover", "project_id": PROJECT, **REFERENCE},
        {"request_mode": "discover", "project_id": PROJECT, "content": "# s"},
        {"request_mode": "discover", "project_id": PROJECT, "after": "a" * 257},
        {"request_mode": "discover"},
    ],
)
def test_invalid_payloads(arguments):
    assert Draft202012Validator(stack_schema()).is_valid(arguments) is False
