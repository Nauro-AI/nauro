"""Exact normal-registry negotiation without granting hosted read integration."""

import copy

import pytest
from nauro_core.mcp_tools import HOSTED_TOOLS

from nauro.sync.decision_reference_contract import DecisionReferenceError, reference_schema
from nauro.sync.reference_reads import negotiate_registry


def registry():
    return {
        "tools": [
            {
                "name": spec["name"],
                "inputSchema": reference_schema()
                if spec["name"] == "propose_decision"
                else copy.deepcopy(spec["input_schema"]),
            }
            for spec in HOSTED_TOOLS
        ]
    }


def test_exact_normal_registry_admits_decisions_but_not_probe_reads():
    assert negotiate_registry(registry()) is False


@pytest.mark.parametrize("change", ["legacy", "extra", "missing", "duplicate", "other_schema"])
def test_changed_normal_registry_is_refused(change):
    value = registry()
    if change == "legacy":
        next(t for t in value["tools"] if t["name"] == "propose_decision")["inputSchema"] = {
            "required": ["rationale"]
        }
    elif change == "extra":
        value["tools"].append({"name": "new_tool", "inputSchema": {}})
    elif change == "missing":
        value["tools"].pop()
    elif change == "duplicate":
        value["tools"][-1] = value["tools"][0]
    else:
        next(t for t in value["tools"] if t["name"] == "get_context")["inputSchema"] = {}
    with pytest.raises(DecisionReferenceError):
        negotiate_registry(value)
