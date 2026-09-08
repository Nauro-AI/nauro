import hashlib
import json

import pytest

from nauro.sync.decision_reference import DecisionReferenceError
from tests.test_judgment_submission import _payload
from tests.test_reference_reads import ACTOR, PROJECT, wire

__all__ = ["wire"]


def observation(index):
    raw = _payload()
    return {
        "version": 1,
        "request": {
            "project_id": PROJECT,
            "actor_id": ACTOR,
            "operation_id": f"decision-request:01K{index:023d}",
            "created_at": "2026-01-01T00:00:00.000000Z",
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


def set_page(wire, rows):
    page = {"version": 1, "requests": rows, "next_after": "opaque-cursor"}
    wire.control["result"] = {
        "content": [{"type": "text", "text": json.dumps(page)}],
        "isError": False,
    }
    return page


@pytest.mark.parametrize("count", [0, 1, 10])
def test_discovery_accepts_old_and_bounded_pages(wire, count):
    page = set_page(wire, [observation(index) for index in range(count)])
    assert wire.transport.propose_decision(project_id=PROJECT, request_mode="discover") == page
    wire.transport.propose_decision(
        project_id=PROJECT, request_mode="discover", after=page["next_after"]
    )
    assert wire.calls[-1][0]["params"]["arguments"]["after"] == "opaque-cursor"


def test_discovery_refuses_more_than_ten_records(wire):
    set_page(wire, [observation(index) for index in range(11)])
    with pytest.raises(DecisionReferenceError) as caught:
        wire.transport.propose_decision(project_id=PROJECT, request_mode="discover")
    assert str(caught.value.__cause__) == "Invalid discovery page"


@pytest.mark.parametrize("index", [0, 5, 9])
def test_each_record_requires_exact_saved_bytes(wire, index):
    rows = [observation(index) for index in range(10)]
    rows[index]["request"]["payload_digest"] = "0" * 64
    set_page(wire, rows)
    with pytest.raises(DecisionReferenceError) as caught:
        wire.transport.propose_decision(project_id=PROJECT, request_mode="discover")
    assert str(caught.value.__cause__) == "Saved bytes differ from digest"
