"""Replacement attempts retain the revision from the writer's installed replica."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from nauro.sync import generation_refresh as refresh
from nauro.sync import generation_writes as writes
from nauro.sync import write_revision
from nauro.sync.generation_session import GenerationTransferSession
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import admitted
from tests.test_generation_responses import fresh_projection
from tests.test_generation_write_delivery import ACTOR, delivery

__all__ = ["delivery", "admitted"]

REPLACEMENTS = [
    ("update_state", {"delta": "Changed state"}),
    ("update_stack", {"content": "Changed stack"}),
]


@pytest.mark.parametrize("operation,content", REPLACEMENTS)
def test_default_revision_is_frozen_before_send(delivery, operation, content):
    session, calls, _ = delivery
    writes.generation_write(operation, content)
    payload = json.loads(json.loads(calls[0].content)["payload_json"])
    assert payload["expected_revision"] == "a" * 64
    writes.capture_write_revision.assert_called_once_with(
        session.binding,
        actor=ACTOR,
        session=session,
        **({"family": "stack"} if operation == "update_stack" else {}),
    )


@pytest.mark.parametrize("operation,content", REPLACEMENTS)
@pytest.mark.parametrize("revision", ["b" * 64, "absent"])
def test_explicit_revision_does_not_capture_another_base(delivery, operation, content, revision):
    _, calls, _ = delivery
    writes.generation_write(operation, {**content, "expected_revision": revision})
    payload = json.loads(json.loads(calls[0].content)["payload_json"])
    assert payload["expected_revision"] == revision
    writes.capture_write_revision.assert_not_called()


@pytest.mark.parametrize("operation,content", REPLACEMENTS)
def test_retry_never_rebases_after_replica_refresh(delivery, operation, content):
    _, calls, behavior = delivery
    behavior["drop"] = True
    result = writes.generation_write(operation, content)
    reference = {key: result[key] for key in ("operation_id", "payload_digest")}
    writes.capture_write_revision.return_value = "b" * 64
    behavior.update(drop=False, status="absent")
    writes.generation_write(operation, {"request_mode": "recover", **reference})
    writes.generation_write(operation, {"request_mode": "retry", **reference})
    assert [request.url.path.split("/")[-1] for request in calls] == [
        "submit",
        "lookup",
        "lookup",
        "submit",
    ]
    bodies = [json.loads(request.content) for request in calls]
    assert bodies == [bodies[0]] * 4
    assert json.loads(bodies[-1]["payload_json"])["expected_revision"] == "a" * 64
    writes.capture_write_revision.assert_called_once()


@pytest.mark.parametrize("operation,content", REPLACEMENTS)
def test_unavailable_replica_never_prepares_or_sends(delivery, operation, content):
    _, calls, _ = delivery
    writes.capture_write_revision.side_effect = ValueError("Replica capture unavailable")
    with pytest.raises(ValueError, match="Replica capture unavailable"):
        writes.generation_write(operation, content)
    assert calls == []
    assert writes.generation_write(operation, {"request_mode": "discover"})["attempts"] == []


@pytest.mark.parametrize("family,path", [("state", "state_current.md"), ("stack", "stack.md")])
@pytest.mark.parametrize("body", [b"Original\r\n", b"", b"Original \xff\n", None])
def test_capture_uses_verified_installed_bytes_without_online_refresh(admitted, family, path, body):
    binding, current, checks = admitted
    artifacts = {"state.md": b"Legacy content must not become the revision"}
    if body is not None:
        artifacts[path] = body
    current[0] = fresh_projection(artifacts)
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    current[0] = fresh_projection({path: b"Newer hosted content"})
    checks.clear()
    session = Mock(spec=GenerationTransferSession)

    revision = write_revision.capture_write_revision(
        binding, actor=USER_ID, session=session, family=family
    )

    assert revision == ("absent" if body is None else hashlib.sha256(body).hexdigest())
    assert checks == []
    session.require_actor.assert_called_with(USER_ID)


@pytest.mark.parametrize("family", ["state", "stack"])
def test_refresh_between_control_capture_and_file_capture_refuses(admitted, monkeypatch, family):
    binding, current, _ = admitted
    capture = write_revision._capture_prepared

    def interrupted(*args, **kwargs):
        current[0] = fresh_projection({"state_current.md": b"Other process refreshed"})
        refresh.recover_generation_refresh(binding, actor=USER_ID)
        return capture(*args, **kwargs)

    monkeypatch.setattr(write_revision, "_capture_prepared", interrupted)
    with pytest.raises(ValueError, match="Refresh evidence changed during capture"):
        write_revision.capture_write_revision(
            binding, actor=USER_ID, session=Mock(spec=GenerationTransferSession), family=family
        )
