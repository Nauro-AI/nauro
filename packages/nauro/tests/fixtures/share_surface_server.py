"""Sharing CLI and registered stdio publish atomic briefs and recover original receipts."""

import hashlib
import json

import pytest
from mcp_server import app as application
from mcp_server.generations import read_generation_pointer
from mcp_server.shared_generations import read_complete
from tests.test_shared_share_publication import (
    local_dynamodb,
    mutation_database,
    production,
    transport,
)
from tests.test_shared_state_client import CLIENT
from tests.test_shared_state_client import run_client as run_state_client
from tests.test_state_transport import _body as state_body

from tests.conftest import TEST_PROJECT_ID

__all__ = ["local_dynamodb", "mutation_database", "production", "transport"]
SHARE_CLIENT = CLIENT.replace(
    '"update_state": ("delta",),', '"share_context": ("slug", "content"),'
)


def run_client(production, home, args, calls, receipts):
    return run_state_client(production, home, args, calls, receipts, script=SHARE_CLIENT)


@pytest.mark.parametrize("surface", ["stdio", "cli"])
@pytest.mark.parametrize("drop", [False, True], ids=["receipt", "restart-recovery"])
def test_public_mutation_and_second_replica(
    production,
    tmp_path,
    monkeypatch,
    surface,
    drop,
):
    monkeypatch.setattr(application, "RATE_LIMIT_PER_SECOND", 10000)
    operation, content, path, expected = (
        "share_context",
        {
            "slug": "verified-brief",
            "content": "Verified brief 雪",
            "pointer_kind": "brief",
            "summary": "Shared verification",
        },
        "context/verified-brief.md",
        "Verified brief 雪",
    )
    before = read_generation_pointer(TEST_PROJECT_ID)
    args = {
        "origin": "https://mcp.nauro.ai",
        "actor": state_body()["expected_user_id"],
        "project": TEST_PROJECT_ID,
        "token": production.token,
        "drop": drop,
        "surface": surface,
        "operation": operation,
        "content": content,
        "path": path,
        "expected": expected,
    }
    writer = {**args, "repo": str(tmp_path / "writer-repo")}
    observer = {**args, "repo": str(tmp_path / "observer-repo")}
    calls, receipts = [], []
    run_client(production, tmp_path / "observer", {**observer, "phase": "attach"}, calls, receipts)
    first = run_client(
        production, tmp_path / "writer", {**writer, "phase": "write"}, calls, receipts
    )
    pointer = read_generation_pointer(TEST_PROJECT_ID)
    assert pointer.generation_id != before.generation_id
    assert pointer.decision_counter == before.decision_counter
    assert len(receipts) == 1
    if drop:
        recovered = run_client(
            production, tmp_path / "writer", {**writer, "phase": "recover"}, calls, receipts
        )
        assert recovered["operation_id"] == first["operation_id"]
        assert recovered["payload_digest"] == first["payload_digest"]
        assert recovered["receipt_json"] == receipts[0]
        assert recovered["read_generation"] == pointer.generation_id
    observed = run_client(
        production, tmp_path / "observer", {**observer, "phase": "refresh"}, calls, receipts
    )
    assert observed["read_generation"] == pointer.generation_id
    assert read_generation_pointer(TEST_PROJECT_ID) == pointer
    artifacts = {a.path: a.content for a in read_complete(pointer).verified.artifacts}
    assert artifacts[path] == expected.encode()
    receipt = json.loads(receipts[0])
    assert receipt["details"]["path"] == path
    assert receipt["details"]["brief_digest"] == hashlib.sha256(artifacts[path]).hexdigest()
    assert (
        receipt["details"]["provenance_digest"]
        == hashlib.sha256(artifacts["questions-provenance.json"]).hexdigest()
    )
    assert path.encode() in artifacts["open-questions.md"]
    assert (
        receipt["details"]["question_event_id"].encode() in artifacts["questions-provenance.json"]
    )
    changed_paths = {"open-questions.md", "questions-provenance.json"}
    for original_path, original_body in production.files.items():
        if original_path not in changed_paths:
            assert artifacts[original_path] == original_body
    assert len([path for path in calls if path.endswith("/submit")]) == 1
