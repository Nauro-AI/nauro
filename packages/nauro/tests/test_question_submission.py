"""Question identities, strict receipts and durable recovery."""

import ast
import errno
import json
import stat
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from filelock import SoftFileLock, Timeout

from nauro.store import question_records as records
from nauro.store.question_contract import (
    QuestionTransportError,
    question_payload,
    resolution_payload,
    verify_question_response,
)
from nauro.store.state_records import list_state_submissions
from nauro.store.submission_records import (
    SubmissionRecordCorruptError,
    SubmissionRecordError,
    list_submissions,
)
from nauro.sync import question_submission as submission
from nauro.sync.question_transport import HttpQuestionTransport
from tests.test_judgment_submission import OTHER, PROJECT, USER, home

__all__ = ["home"]
GENERATION = "01K00000000000000000000004"


def _prepared(action="append"):
    payload = (
        question_payload("Question?") if action == "append" else resolution_payload(("Q1",), "D42")
    )
    return records.prepare_question_submission(PROJECT, USER, payload)


def _body(record, status="committed"):
    action = "resolve" if "resolved_by" in json.loads(record.payload_json) else "append"
    body = {
        "version": 1,
        "scope": record.scope.model_dump(),
        "payload_digest": record.payload_digest,
        "action": action,
        "status": status,
        "unresolved": status in {"absent", "no_change_observed"},
    }
    if status == "committed":
        details = {
            "snapshot_key": f"generations/{PROJECT}/{GENERATION}/snapshot.json",
            "snapshot_digest": "c" * 64,
        }
        timestamp = "2026-09-06T00:00:00.000000Z"
        details.update(
            {"question_event_id": GENERATION, "question_created_at": timestamp}
            if action == "append"
            else {"resolution_row_digest": "a" * 64}
        )
        receipt = {
            "receipt_id": GENERATION,
            "operation_kind": "flag_question",
            "operation_id": record.scope.operation_id,
            "result": "committed" if action == "append" else "resolved",
            "committed_at": timestamp,
            "artifact_digest": "a" * 64,
            "generation_id": GENERATION,
            "target_kind": "question" if action == "append" else "question_resolution",
            "target_id": "Q2" if action == "append" else GENERATION,
            "details": details,
        }
        body["receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if action == "resolve" and status in {"committed", "no_change_observed"}:
        body["diagnostics"] = {
            "requested_question_ids": ["Q1"],
            "resolved_question_ids": ["Q1"] if status == "committed" else [],
            "resolved_by": "D42",
            "relocated_ids": [],
            "skipped_prose_ids": [],
        }
    return body


def _result(record, status="committed"):
    return verify_question_response(
        json.dumps(_body(record, status)).encode(), record.scope, record.payload_json
    )


def test_distinct_identities_and_separate_discovery(home):
    first, second, resolution = _prepared(), _prepared(), _prepared("resolve")
    assert (
        len({first.scope.operation_id, second.scope.operation_id, resolution.scope.operation_id})
        == 3
    )
    assert first.payload_json == second.payload_json
    assert first.scope.operation_kind == resolution.scope.operation_kind == "flag_question"
    assert records.list_question_submissions(PROJECT, USER) == (first, second, resolution)
    assert list_submissions(PROJECT, USER) == ()
    assert list_state_submissions(PROJECT, USER) == ()
    assert records._record_path(first.scope).stat().st_mode & 0o777 == 0o600


def test_observation_is_not_cached_as_completion(home):
    record = _prepared("resolve")
    transport = Mock(
        submit=Mock(return_value=_result(record, "no_change_observed")),
        lookup=Mock(return_value=_result(record)),
    )
    assert submission.submit_question(record.scope, transport).unresolved is True
    assert records.read_question_submission(record.scope).phase == "uncertain"
    with pytest.raises(submission.QuestionRecoveryRequiredError):
        submission.submit_question(record.scope, transport)
    committed = submission.recover_question(record.scope, transport)
    assert committed.unresolved is False
    assert submission.recover_question(record.scope, transport) == committed
    transport.submit.assert_called_once()
    transport.lookup.assert_called_once()


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_retry_observes_absence_before_exact_resend(home, action):
    record = _prepared(action)
    events = []

    def lookup(saved):
        events.append("lookup")
        return _result(record, "absent")

    def send(saved):
        events.append("submit")
        assert saved.scope == record.scope
        assert saved.payload_json == record.payload_json
        assert saved.phase == "uncertain"
        return _result(record)

    result = submission.retry_question(record.scope, Mock(lookup=lookup, submit=send))
    assert result.status == "committed"
    assert events == ["lookup", "submit"]


@pytest.mark.parametrize("seconds,allowed", [(-1, False), (0, True), (86400, True), (86401, False)])
def test_resend_horizon_after_lookup(home, monkeypatch, seconds, allowed):
    record = _prepared()
    now = datetime.fromisoformat(record.created_at.replace("Z", "+00:00")) + timedelta(
        seconds=seconds
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return now

    monkeypatch.setattr(submission, "datetime", Clock)
    transport = Mock(
        lookup=Mock(return_value=_result(record, "absent")),
        submit=Mock(return_value=_result(record)),
    )
    if allowed:
        assert submission.retry_question(record.scope, transport).status == "committed"
    else:
        with pytest.raises(submission.QuestionRetryExpiredError):
            submission.retry_question(record.scope, transport)
        transport.submit.assert_not_called()
    transport.lookup.assert_called_once()


@pytest.mark.parametrize("action", ["append", "resolve"])
@pytest.mark.parametrize(
    "field,value",
    [("version", True), ("unresolved", 0), ("payload_digest", "0" * 64), ("extra", "unknown")],
)
def test_response_binding_and_closed_fields(home, action, field, value):
    record = _prepared(action)
    body = {**_body(record), field: value}
    with pytest.raises(QuestionTransportError):
        verify_question_response(json.dumps(body).encode(), record.scope, record.payload_json)


@pytest.mark.parametrize("action", ["append", "resolve"])
@pytest.mark.parametrize(
    "mutation", ["operation", "target", "details", "timestamp", "snapshot", "canonical"]
)
def test_receipt_binding(home, action, mutation):
    record = _prepared(action)
    body = _body(record)
    receipt = json.loads(body["receipt_json"])
    if mutation == "operation":
        receipt["operation_id"] = "wrong-operation"
    elif mutation == "target":
        receipt["target_kind"] = "curated_state"
    elif mutation == "details":
        receipt["details"]["unexpected"] = "extra"
    elif mutation == "timestamp":
        receipt["committed_at"] = "not-a-date"
    elif mutation == "snapshot":
        receipt["details"]["snapshot_key"] = "wrong/snapshot.json"
    body["receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + (
        " " if mutation == "canonical" else ""
    )
    with pytest.raises(QuestionTransportError):
        verify_question_response(json.dumps(body).encode(), record.scope, record.payload_json)


def test_action_diagnostics_and_lookup_observations_bind_request(home):
    record = _prepared("resolve")
    for mutation in ("action", "diagnostics", "effects", "lookup"):
        body = _body(record, "no_change_observed")
        if mutation == "action":
            body["action"] = "append"
        elif mutation == "diagnostics":
            body["diagnostics"]["resolved_by"] = "D43"
        elif mutation == "effects":
            body["diagnostics"]["resolved_question_ids"] = ["Q1"]
        with pytest.raises(QuestionTransportError):
            verify_question_response(
                json.dumps(body).encode(),
                record.scope,
                record.payload_json,
                lookup=mutation == "lookup",
            )


@pytest.mark.parametrize("status", ["committed", "expired", "digest_conflict"])
def test_terminal_lookup_prevents_retry(home, status):
    record = _prepared()
    transport = Mock(lookup=Mock(return_value=_result(record, status)))
    assert submission.retry_question(record.scope, transport).status == status
    transport.submit.assert_not_called()


def test_transport_preserves_body_and_refuses_redirect(home):
    record = _prepared()
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=_body(record, "absent"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        adapter = HttpQuestionTransport("https://example.test", client)
        assert adapter.lookup(record).status == "absent"
    assert seen[0].url.path == "/questions/lookup"
    assert json.loads(seen[0].content)["payload_json"] == record.payload_json
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(307, headers={"Location": "https://other.test"})
        )
    ) as client:
        with pytest.raises(QuestionTransportError):
            HttpQuestionTransport("https://example.test", client).submit(record)


def test_no_production_consumers():
    root = Path(__file__).parents[1] / "src" / "nauro"
    modules = {
        "nauro.mcp.question_responses",
        "nauro.store.question_contract",
        "nauro.store.question_records",
        "nauro.sync.question_submission",
        "nauro.sync.question_transport",
    }
    found = []
    for path in root.rglob("*.py"):
        own = "nauro." + ".".join(path.relative_to(root).with_suffix("").parts)
        if own in modules:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module} | {f"{node.module}.{a.name}" for a in node.names}
            else:
                continue
            if names & modules:
                found.append(str(path.relative_to(root)))
    assert found == []


def test_file_barrier_failure_prevents_send(home, monkeypatch):
    record = _prepared()
    original = records.os.fsync

    def fail(fd):
        if stat.S_ISREG(records.os.fstat(fd).st_mode):
            raise OSError(errno.ENOSPC, "synthetic full disk")
        original(fd)

    monkeypatch.setattr(records.os, "fsync", fail)
    transport = Mock()
    with pytest.raises(OSError) as error:
        submission.submit_question(record.scope, transport)
    assert error.value.errno == errno.ENOSPC
    transport.submit.assert_not_called()
    assert records.read_question_submission(record.scope) == record


def test_failed_namespace_barrier_is_repeated_before_network(home, monkeypatch):
    record = _prepared()
    original_replace = records.os.replace
    original_sync = records._directory_sync

    def replace(source, target):
        original_replace(source, target)
        monkeypatch.setattr(
            records, "_directory_sync", Mock(side_effect=OSError(errno.EIO, "sync"))
        )

    monkeypatch.setattr(records.os, "replace", replace)
    transport = Mock()
    with pytest.raises(OSError):
        submission.submit_question(record.scope, transport)
    with pytest.raises(OSError):
        submission.recover_question(record.scope, transport)
    transport.submit.assert_not_called()
    transport.lookup.assert_not_called()
    monkeypatch.setattr(records, "_directory_sync", original_sync)
    monkeypatch.setattr(records.os, "replace", original_replace)
    transport.lookup.return_value = _result(record, "absent")
    assert submission.recover_question(record.scope, transport).status == "absent"
    transport.submit.assert_not_called()


def test_failed_receipt_persistence_leaves_recovery_available(home, monkeypatch):
    record = _prepared()
    original = records._write

    def fail(saved):
        if saved.phase == "resolved":
            raise OSError(errno.EIO, "receipt persistence")
        original(saved)

    monkeypatch.setattr(records, "_write", fail)
    transport = Mock(
        submit=Mock(return_value=_result(record)), lookup=Mock(return_value=_result(record))
    )
    with pytest.raises(OSError):
        submission.submit_question(record.scope, transport)
    assert records.read_question_submission(record.scope).phase == "uncertain"
    monkeypatch.setattr(records, "_write", original)
    assert submission.recover_question(record.scope, transport).status == "committed"
    transport.submit.assert_called_once()


def test_native_lock_and_corruption_refuse_before_network(home, monkeypatch):
    record = _prepared()
    with records.question_submission_lock(record.scope):
        with pytest.raises(Timeout):
            with records.question_submission_lock(record.scope):
                pass
    monkeypatch.setattr(records, "FileLock", SoftFileLock)
    with pytest.raises(SubmissionRecordError):
        with records.question_submission_lock(record.scope):
            pass
    path = records._record_path(record.scope)
    raw = json.loads(path.read_bytes())
    raw["record"]["payload_json"] = "{}"
    path.write_text(json.dumps(raw))
    with pytest.raises(SubmissionRecordCorruptError):
        records.read_question_submission(record.scope)


def test_account_change_during_transport_preserves_uncertainty(home):
    record = _prepared()

    def switch(saved):
        (home / "config.json").write_text(
            json.dumps({"auth": {"user_id": OTHER, "access_token": "synthetic-test-token"}})
        )
        return _result(record)

    with pytest.raises(SubmissionRecordError):
        submission.submit_question(record.scope, Mock(submit=switch))
    (home / "config.json").write_text(
        json.dumps({"auth": {"user_id": USER, "access_token": "synthetic-test-token"}})
    )
    assert records.read_question_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("action", ["append", "resolve"])
def test_lookup_observation_and_unbound_receipt_never_persist(home, action):
    record = _prepared(action)
    invalid = _result(record).model_copy(update={"receipt_json": "invalid"})
    with pytest.raises(QuestionTransportError):
        submission.recover_question(record.scope, Mock(lookup=Mock(return_value=invalid)))
    assert records.read_question_submission(record.scope) == record


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.test",
        "https://user@example.test",
        "https://example.test/path",
        "https://example.test?query=1",
    ],
)
def test_untrusted_origins_refuse(origin):
    with httpx.Client() as client:
        with pytest.raises(QuestionTransportError):
            HttpQuestionTransport(origin, client)


def test_duplicate_and_oversized_responses_refuse(home):
    record = _prepared()
    duplicate = json.dumps(_body(record))[:-1] + ',"version":1}'
    for raw in (duplicate.encode(), b" " * (64 * 1024 + 1)):
        with pytest.raises(QuestionTransportError):
            verify_question_response(raw, record.scope, record.payload_json)


def test_payload_normalization_preserves_order_and_duplicates():
    assert question_payload("Question?", targets=("Q02", "Q1", "Q02")) == (
        b'{"context":null,"question":"Question?","targets":["Q2","Q1","Q2"]}\n'
    )
    assert resolution_payload(("Q02", "Q1", "Q02"), "D42") == (
        b'{"action":"resolve","resolved_by":"D42","targets":["Q2","Q1","Q2"]}\n'
    )


def test_missing_record_is_not_absence(home):
    record = _prepared()
    records._record_path(record.scope).unlink()
    transport = Mock()
    with pytest.raises(SubmissionRecordError):
        submission.recover_question(record.scope, transport)
    transport.lookup.assert_not_called()


def test_terminal_record_cannot_be_reopened(home):
    record = _prepared()
    submission.submit_question(record.scope, Mock(submit=Mock(return_value=_result(record))))
    saved = records.read_question_submission(record.scope)
    with pytest.raises(SubmissionRecordError):
        records.mark_question_uncertain(saved)
    with pytest.raises(SubmissionRecordError):
        records.record_question_result(saved, _result(record, "absent"))
