"""Known ordinary registry profiles retain exact schema negotiation."""

import hashlib
import json
from pathlib import Path

import pytest

from nauro.sync.decision_reference_contract import DecisionReferenceError
from nauro.sync.reference_reads import negotiate_registry
from tests.test_normal_reference_registry import registry
from tests.test_reference_reads import PROJECT, wire

__all__ = ["wire"]
FIXTURE = Path(__file__).parent / "fixtures" / "hosted_question_schema.json"


def question_schema():
    return json.loads(FIXTURE.read_text())


def ordinary(questions):
    value = registry()
    if questions:
        next(t for t in value["tools"] if t["name"] == "flag_question")["inputSchema"] = (
            question_schema()
        )
    return value


def test_schema_matches_independently_reviewed_fixture():
    from nauro.sync.hosted_question_schema import question_schema as implementation

    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "34464a6a88ed893e8c8d475dc495004eaad129e449fecead5de1865cf88db781"
    )
    assert implementation() == question_schema()


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_real_transport_preserves_decisions_and_read_limits(wire, questions, reversed_order):
    value = ordinary(questions)
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


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("oneOf", 0, "required"), []),
        (("oneOf", 0, "oneOf", 1, "required"), ["resolved_by"]),
        (("oneOf", 1, "not", "anyOf"), []),
        (("oneOf", 2, "properties", "request_mode", "const"), "recover"),
        (("properties", "operation_id", "pattern"), ".*"),
        (("properties", "operation_id", "maxLength"), 44),
        (("properties", "payload_digest", "minLength"), 63),
        (("properties", "request_mode", "enum"), ["submit"]),
        (("additionalProperties",), True),
    ],
)
def test_nested_schema_drift_refuses_before_tool_call(wire, path, replacement):
    value = ordinary(True)
    target = next(t for t in value["tools"] if t["name"] == "flag_question")["inputSchema"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    assert_refused(wire, value)


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("change", ["other_schema", "duplicate", "missing", "rename", "pagination"])
def test_registry_shape_stays_closed(wire, questions, change):
    value = ordinary(questions)
    if change == "other_schema":
        value["tools"][0]["inputSchema"] = {}
    elif change == "duplicate":
        value["tools"][-1] = value["tools"][0]
    elif change == "missing":
        value["tools"].pop()
    elif change == "rename":
        value["tools"][-1]["name"] = "unknown"
    else:
        value["nextCursor"] = None
    assert_refused(wire, value)


def assert_refused(wire, value):
    wire.control["registry"] = value
    with pytest.raises(DecisionReferenceError, match="registry"):
        wire.transport.initialize()
    assert wire.transport._initialized is False
    assert wire.transport.reads_available is False
    assert [call[0]["method"] for call in wire.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
