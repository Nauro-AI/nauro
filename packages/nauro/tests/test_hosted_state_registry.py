"""Exact state registry negotiation preserves existing connection boundaries."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from nauro.sync.decision_reference_contract import DecisionReferenceError
from nauro.sync.reference_reads import negotiate_registry
from tests.test_hosted_question_registry import assert_refused, ordinary
from tests.test_reference_reads import PROJECT, wire

__all__ = ["wire"]
FIXTURE = Path(__file__).parent / "fixtures" / "hosted_state_schema.json"
REVISION = "a" * 64
REFERENCE = {"operation_id": "state-request:01M44W51SX9R69FPV6B07A2DS0", "payload_digest": REVISION}


def state_schema():
    return json.loads(FIXTURE.read_text())


def registry(questions, state):
    value = ordinary(questions)
    if state:
        next(t for t in value["tools"] if t["name"] == "update_state")["inputSchema"] = (
            state_schema()
        )
    return value


def test_schema_matches_reviewed_fixture():
    from nauro.sync.hosted_state_schema import state_schema as implementation

    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "a239ef5a1674bda688a5387faecb8ca6e28a131e293aa9c38747dc42e25c82dc"
    )
    assert implementation() == state_schema()
    Draft202012Validator.check_schema(implementation())


@pytest.mark.parametrize("questions", [False, True])
@pytest.mark.parametrize("state", [False, True])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_transport_preserves_decisions_and_read_limits(wire, questions, state, reversed_order):
    value = registry(questions, state)
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
@pytest.mark.parametrize(
    "change", ["other_schema", "duplicate", "missing", "rename", "pagination", "extra"]
)
def test_registry_remains_closed(wire, questions, state, change):
    value = registry(questions, state)
    if change == "other_schema":
        next(t for t in value["tools"] if t["name"] == "get_context")["inputSchema"] = {}
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


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("properties", "expected_revision", "anyOf", 1, "maxLength"), 65),
        (("properties", "expected_revision", "description"), "Use latest revision"),
        (("properties", "operation_id", "pattern"), ".*"),
        (("properties", "operation_id", "minLength"), 39),
        (("properties", "payload_digest", "maxLength"), 65),
        (("properties", "request_mode", "enum"), ["submit"]),
        (("oneOf", 0, "required"), ["delta"]),
        (("oneOf", 1, "propertyNames", "enum"), ["delta"]),
        (("oneOf", 2, "properties", "request_mode", "const"), "retry"),
        (("oneOf", 3, "required"), ["request_mode"]),
        (("oneOf", 4, "propertyNames", "enum"), ["project_id"]),
        (("oneOf", 5, "not", "anyOf"), []),
    ],
)
def test_nested_drift_refuses_before_call(wire, path, replacement):
    value = registry(True, True)
    target = next(t for t in value["tools"] if t["name"] == "update_state")["inputSchema"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    assert_refused(wire, value)


@pytest.mark.parametrize(
    "arguments",
    [
        {"delta": "legacy"},
        {"delta": "legacy", "project_id": PROJECT, "legacy_field": 1},
        *[
            {"delta": "replacement", "expected_revision": revision}
            for revision in (REVISION, "absent")
        ],
        *[
            {
                "request_mode": "submit",
                "delta": "replacement",
                "expected_revision": revision,
                "project_id": PROJECT,
            }
            for revision in (REVISION, "absent")
        ],
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE}
            for mode in ("recover", "retry")
        ],
        {"request_mode": "discover", "project_id": PROJECT},
        {"request_mode": "discover", "project_id": PROJECT, "after": "cursor"},
        {
            "request_mode": "recover",
            "project_id": PROJECT,
            **REFERENCE,
            "operation_id": "state-request:7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        },
    ],
)
def test_supported_payloads(arguments):
    assert list(Draft202012Validator(state_schema()).iter_errors(arguments)) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        *[
            {"delta": "text", "request_mode": mode}
            for mode in (None, "null", "", "unknown", "submit")
        ],
        *[
            {"delta": "text", "expected_revision": revision}
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
        {"delta": "text", "expected_revision": REVISION, "unknown": 1},
        {"request_mode": "submit", "delta": "text", "expected_revision": REVISION, **REFERENCE},
        *[
            {"request_mode": mode, "project_id": PROJECT, **REFERENCE, "delta": "text"}
            for mode in ("recover", "retry")
        ],
        *[
            {
                "request_mode": mode,
                "project_id": PROJECT,
                **REFERENCE,
                "expected_revision": REVISION,
            }
            for mode in ("recover", "retry")
        ],
        {"request_mode": "recover", **REFERENCE},
        {
            "request_mode": "recover",
            "project_id": PROJECT,
            "operation_id": REFERENCE["operation_id"],
        },
        *[
            {
                "request_mode": "recover",
                "project_id": PROJECT,
                **REFERENCE,
                "operation_id": operation,
            }
            for operation in (
                "state-request:8" + "0" * 25,
                "state-request:" + "I" * 26,
                REFERENCE["operation_id"] + "\n",
                "question-request:" + "0" * 26,
            )
        ],
        *[
            {
                "request_mode": "recover",
                "project_id": PROJECT,
                **REFERENCE,
                "payload_digest": digest,
            }
            for digest in (None, "A" * 64, REVISION + "\n", "a" * 63)
        ],
        {"request_mode": "discover", "project_id": PROJECT, **REFERENCE},
        {"request_mode": "discover", "project_id": PROJECT, "delta": "text"},
        {"request_mode": "discover", "project_id": PROJECT, "after": "a" * 257},
        *[
            {"delta": "legacy", name: "value"}
            for name in ("operation_id", "payload_digest", "after")
        ],
    ],
)
def test_invalid_payloads(arguments):
    assert Draft202012Validator(state_schema()).is_valid(arguments) is False
