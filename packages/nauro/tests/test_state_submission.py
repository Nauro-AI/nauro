"""State identity durability and nonterminal observation recovery."""

import errno
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from filelock import SoftFileLock, Timeout

from nauro.store import state_records as records
from nauro.store.state_contract import StateTransportError, state_payload, verify_state_response
from nauro.store.submission_records import (
    SubmissionActorMismatchError,
    SubmissionRecordCorruptError,
    SubmissionRecordError,
    list_submissions,
)
from nauro.sync import state_submission as submission
from nauro.sync.state_transport import HttpStateTransport
from tests.test_judgment_submission import OTHER, PROJECT, USER, home

__all__ = ["home"]
GENERATION = "01K00000000000000000000004"
BASE = "01K00000000000000000000005"


def _prepared(expected_revision=None):
    return records.prepare_state_submission(
        PROJECT, USER, state_payload("Frozen state", expected_revision)
    )


def _body(record, status="committed"):
    result = {
        "version": 1,
        "scope": record.scope.model_dump(),
        "payload_digest": record.payload_digest,
        "status": status,
        "unresolved": status in {"absent", "noop_observed", "revision_conflict_observed"},
    }
    if status == "revision_conflict_observed":
        result.update(
            expected_revision=json.loads(record.payload_json)["expected_revision"],
            current_revision="b" * 64,
        )
    if status == "committed":
        receipt = {
            "receipt_id": GENERATION,
            "operation_kind": "update_state",
            "operation_id": record.scope.operation_id,
            "result": "committed",
            "committed_at": "2026-09-06T00:00:00.000000Z",
            "artifact_digest": "a" * 64,
            "generation_id": GENERATION,
            "target_kind": "curated_state",
            "target_id": "state_current.md",
            "details": {
                "base_generation_id": BASE,
                "base_manifest_digest": "a" * 64,
                "base_snapshot_digest": "a" * 64,
                "source_revision": "a" * 64,
                "previous_revision": json.loads(record.payload_json)["expected_revision"]
                or "a" * 64,
                "state_revision": "b" * 64,
                "previous_history_revision": "absent",
                "history_revision": "b" * 64,
                "snapshot_key": f"generations/{PROJECT}/{GENERATION}/snapshot.json",
                "snapshot_digest": "c" * 64,
            },
        }
        result.update(
            receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")), warning=None
        )
    return result


def _result(record, status="committed"):
    return verify_state_response(
        json.dumps(_body(record, status)).encode(), record.scope, record.payload_json
    )


def test_private_records_and_distinct_new_identities(home):
    first, second = _prepared(), _prepared()
    changed = records.prepare_state_submission(PROJECT, USER, state_payload("Changed", "a" * 64))
    assert (
        len({first.scope.operation_id, second.scope.operation_id, changed.scope.operation_id}) == 3
    )
    assert first.payload_json == second.payload_json
    path = records._record_path(first.scope)
    assert path.is_relative_to(home / "submission-records" / "state")
    assert path.stat().st_mode & 0o777 == 0o600
    assert records.list_state_submissions(PROJECT, USER) == (first, second, changed)
    assert list_submissions(PROJECT, USER) == ()


@pytest.mark.parametrize("status", ["noop_observed", "revision_conflict_observed"])
def test_observation_recovers_later_commit_without_resubmit(home, status):
    record = _prepared("a" * 64)
    transport = Mock()
    transport.submit.return_value = _result(record, status)
    observed = submission.submit_state(record.scope, transport)
    assert observed.unresolved is True
    assert records.read_state_submission(record.scope).phase == "uncertain"
    with pytest.raises(submission.StateRecoveryRequiredError):
        submission.submit_state(record.scope, transport)
    transport.lookup.return_value = _result(record)
    recovered = submission.recover_state(record.scope, transport)
    assert recovered == _result(record)
    assert recovered.unresolved is False
    assert records.read_state_submission(record.scope).phase == "resolved"
    assert submission.recover_state(record.scope, transport) == recovered
    transport.submit.assert_called_once()
    transport.lookup.assert_called_once()


def test_explicit_retry_looks_up_first_and_preserves_original_bytes(home):
    record = _prepared()
    calls = []

    def lookup(saved):
        calls.append("lookup")
        assert saved.payload_json == record.payload_json
        return _result(record, "absent")

    def send(saved):
        calls.append("send")
        assert saved.scope == record.scope
        assert saved.payload_json == record.payload_json
        assert saved.payload_digest == record.payload_digest
        assert records.read_state_submission(record.scope) == saved
        assert saved.phase == "uncertain"
        return _result(record, "noop_observed")

    result = submission.retry_state(record.scope, Mock(lookup=lookup, submit=send))
    assert calls == ["lookup", "send"]
    assert result.unresolved is True


@pytest.mark.parametrize("status", ["committed", "expired", "digest_conflict"])
def test_retry_never_sends_for_non_absent_lookup(home, status):
    record = _prepared()
    transport = Mock(lookup=Mock(return_value=_result(record, status)))
    assert submission.retry_state(record.scope, transport) == _result(record, status)
    transport.submit.assert_not_called()


def test_recovery_of_absence_never_sends(home):
    record = _prepared()
    transport = Mock(lookup=Mock(return_value=_result(record, "absent")))
    result = submission.recover_state(record.scope, transport)
    assert result.unresolved is True
    assert result.status == "absent"
    transport.submit.assert_not_called()


@pytest.mark.parametrize("status", ["noop_observed", "revision_conflict_observed"])
def test_lookup_cannot_return_submit_observations(home, status):
    record = _prepared("a" * 64)
    transport = Mock(lookup=Mock(return_value=_result(record, status)))
    with pytest.raises(StateTransportError):
        submission.retry_state(record.scope, transport)
    transport.submit.assert_not_called()


@pytest.mark.parametrize(
    "elapsed,allowed",
    [
        (timedelta(hours=24), True),
        (timedelta(hours=24, microseconds=1), False),
        (timedelta(microseconds=-1), False),
    ],
)
def test_exact_resend_horizon(home, monkeypatch, elapsed, allowed):
    record = _prepared()
    now = datetime.now(timezone.utc)
    created = (now - elapsed).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    record = records.StateSubmission.model_validate({**record.model_dump(), "created_at": created})
    records._write(record)

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz):
            return now

    monkeypatch.setattr(submission, "datetime", Clock)
    transport = Mock(
        lookup=Mock(return_value=_result(record, "absent")),
        submit=Mock(return_value=_result(record)),
    )
    if allowed:
        assert submission.retry_state(record.scope, transport).status == "committed"
    else:
        with pytest.raises(submission.StateRetryExpiredError):
            submission.retry_state(record.scope, transport)
        transport.submit.assert_not_called()
    transport.lookup.assert_called_once()


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
        submission.submit_state(record.scope, transport)
    assert error.value.errno == errno.ENOSPC
    transport.submit.assert_not_called()
    assert records.read_state_submission(record.scope) == record


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
        submission.submit_state(record.scope, transport)
    with pytest.raises(OSError):
        submission.recover_state(record.scope, transport)
    transport.submit.assert_not_called()
    transport.lookup.assert_not_called()
    monkeypatch.setattr(records, "_directory_sync", original_sync)
    monkeypatch.setattr(records.os, "replace", original_replace)
    transport.lookup.return_value = _result(record, "absent")
    assert submission.recover_state(record.scope, transport).status == "absent"
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
        submission.submit_state(record.scope, transport)
    assert records.read_state_submission(record.scope).phase == "uncertain"
    monkeypatch.setattr(records, "_write", original)
    assert submission.recover_state(record.scope, transport).status == "committed"
    transport.submit.assert_called_once()


def test_native_lock_and_corruption_refuse_before_network(home, monkeypatch):
    record = _prepared()
    with records.state_submission_lock(record.scope):
        with pytest.raises(Timeout):
            with records.state_submission_lock(record.scope):
                pass
    monkeypatch.setattr(records, "FileLock", SoftFileLock)
    with pytest.raises(SubmissionRecordError):
        with records.state_submission_lock(record.scope):
            pass
    path = records._record_path(record.scope)
    raw = json.loads(path.read_bytes())
    raw["record"]["payload_json"] = "{}"
    path.write_text(json.dumps(raw))
    with pytest.raises(SubmissionRecordCorruptError):
        records.read_state_submission(record.scope)


def test_restart_recovers_observation_without_send(home):
    record = _prepared()
    submission.submit_state(
        record.scope, Mock(submit=Mock(return_value=_result(record, "noop_observed")))
    )
    script = """
import json,sys
from nauro.store.state_records import list_state_submissions
from nauro.store.state_contract import verify_state_response
from nauro.sync.state_submission import recover_state
record,=list_state_submissions(sys.argv[1],sys.argv[2])
class Transport:
    def submit(self,record): raise AssertionError("recovery submitted")
    def lookup(self,saved):
        assert saved == record
        return verify_state_response(
            sys.stdin.buffer.read(),record.scope,record.payload_json,lookup=True)
result=recover_state(record.scope,Transport())
print(result.model_dump_json())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, PROJECT, USER],
        input=json.dumps(_body(record)).encode(),
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == _body(record)
    assert records.read_state_submission(record.scope).phase == "resolved"


@pytest.mark.parametrize(
    "field,value",
    [("version", True), ("unresolved", 0), ("payload_digest", "0" * 64), ("extra", 1)],
)
def test_response_binding_is_strict(home, field, value):
    record = _prepared()
    body = _body(record)
    body[field] = value
    with pytest.raises(StateTransportError):
        verify_state_response(json.dumps(body).encode(), record.scope, record.payload_json)


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "other"),
        ("target_id", "project.md"),
        ("target_kind", "decision"),
        ("generation_id", BASE),
        ("artifact_digest", "invalid"),
        ("committed_at", "yesterday"),
        ("receipt_id", "invalid"),
    ],
)
def test_receipt_fields_are_verified(home, field, value):
    record = _prepared()
    body = _body(record)
    receipt = json.loads(body["receipt_json"])
    receipt[field] = value
    body["receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    with pytest.raises(StateTransportError):
        verify_state_response(json.dumps(body).encode(), record.scope, record.payload_json)


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_key", "other/snapshot.json"),
        ("previous_revision", "b" * 64),
        ("state_revision", "absent"),
        ("source_revision", "invalid"),
        ("extra", "x"),
    ],
)
def test_receipt_details_are_closed_and_bound(home, field, value):
    record = _prepared("a" * 64)
    body = _body(record)
    receipt = json.loads(body["receipt_json"])
    receipt["details"][field] = value
    body["receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    with pytest.raises(StateTransportError):
        verify_state_response(json.dumps(body).encode(), record.scope, record.payload_json)


def test_noncanonical_receipt_and_duplicate_response_keys_refuse(home):
    record = _prepared()
    body = _body(record)
    body["receipt_json"] += " "
    with pytest.raises(StateTransportError):
        verify_state_response(json.dumps(body).encode(), record.scope, record.payload_json)
    duplicate = json.dumps(_body(record))[:-1] + ',"version":1}'
    with pytest.raises(StateTransportError):
        verify_state_response(duplicate.encode(), record.scope, record.payload_json)


def test_https_adapter_retains_identity_and_rechecks_account(home):
    record = _prepared()
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content) == {
            "version": 1,
            "project_id": PROJECT,
            "expected_user_id": USER,
            "operation_id": record.scope.operation_id,
            "payload_digest": record.payload_digest,
            "payload_json": record.payload_json,
        }
        return httpx.Response(200, json=_body(record, "absent"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HttpStateTransport("https://example.test", client)
        assert transport.lookup(record).status == "absent"
        assert str(calls[0].url) == "https://example.test/state/lookup"
        (home / "config.json").write_text(
            json.dumps({"auth": {"user_id": OTHER, "access_token": "other"}})
        )
        with pytest.raises(SubmissionActorMismatchError):
            transport.lookup(record)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user@example.test",
        "https://example.test/path",
        "https://example.test/?q=x",
    ],
)
def test_untrusted_origin_refuses(url):
    with httpx.Client() as client:
        with pytest.raises(StateTransportError):
            HttpStateTransport(url, client)


@pytest.mark.parametrize("code", [302, 401, 503])
def test_http_errors_never_retry_or_redirect(home, code):
    record = _prepared()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, headers={"Location": "https://other.test"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(StateTransportError):
            submission.submit_state(
                record.scope, HttpStateTransport("https://example.test", client)
            )
    assert len(calls) == 1
    assert records.read_state_submission(record.scope).phase == "uncertain"


def test_durable_send_and_receipt_barriers(home, monkeypatch):
    record = _prepared()
    events = []
    original = records.os.fsync

    def sync(fd):
        original(fd)
        events.append("file" if stat.S_ISREG(records.os.fstat(fd).st_mode) else "directory")

    def send(saved):
        assert events[-2:] == ["file", "directory"]
        assert records.read_state_submission(record.scope) == saved
        events.append("send")
        return _result(record)

    monkeypatch.setattr(records.os, "fsync", sync)
    assert submission.submit_state(record.scope, Mock(submit=send)).status == "committed"
    assert events[-2:] == ["file", "directory"]
    assert events.count("send") == 1


def test_unreadable_record_is_not_absence(home, monkeypatch):
    record = _prepared()
    path = records._record_path(record.scope)
    original = records.os.open

    def denied(target, flags, *args, **kwargs):
        if target == path:
            raise PermissionError(errno.EACCES, "synthetic access refusal")
        return original(target, flags, *args, **kwargs)

    monkeypatch.setattr(records.os, "open", denied)
    transport = Mock()
    with pytest.raises(PermissionError) as error:
        submission.recover_state(record.scope, transport)
    assert error.value.errno == errno.EACCES
    transport.lookup.assert_not_called()
    transport.submit.assert_not_called()


def test_changed_account_during_response_retains_uncertain_record(home):
    record = _prepared()

    def handler(request):
        (home / "config.json").write_text(
            json.dumps({"auth": {"user_id": OTHER, "access_token": "other"}})
        )
        return httpx.Response(200, json=_body(record))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SubmissionActorMismatchError):
            submission.submit_state(
                record.scope, HttpStateTransport("https://example.test", client)
            )
    (home / "config.json").write_text(
        json.dumps({"auth": {"user_id": USER, "access_token": "synthetic-test-token"}})
    )
    assert records.read_state_submission(record.scope).phase == "uncertain"


def test_response_size_limit_retains_uncertainty(home):
    record = _prepared()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * (64 * 1024 + 1))
        )
    ) as client:
        with pytest.raises(StateTransportError):
            submission.submit_state(
                record.scope, HttpStateTransport("https://example.test", client)
            )
    assert records.read_state_submission(record.scope).phase == "uncertain"


def test_invalid_payload_cannot_create_saved_identity(home):
    with pytest.raises(ValueError):
        records.prepare_state_submission(
            PROJECT, USER, b' {"delta":"x","expected_revision":null,"operation":"update_state"}'
        )
    assert records.list_state_submissions(PROJECT, USER) == ()


def test_resolved_record_cannot_be_reopened(home):
    record = _prepared()
    submission.submit_state(record.scope, Mock(submit=Mock(return_value=_result(record))))
    saved = records.read_state_submission(record.scope)
    with pytest.raises(SubmissionRecordError):
        records.mark_state_uncertain(saved)
    with pytest.raises(SubmissionRecordError):
        records.record_state_result(saved, _result(record, "absent"))


def test_state_modules_have_no_production_consumers():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "nauro"
    modules = {
        "nauro.store.state_contract",
        "nauro.store.state_records",
        "nauro.sync.state_submission",
        "nauro.sync.state_transport",
    }
    found = []
    for path in root.rglob("*.py"):
        own = "nauro." + ".".join(path.relative_to(root).with_suffix("").parts)
        if own in modules:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imports = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imports = {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
            else:
                continue
            if imports & modules:
                found.append(str(path.relative_to(root)))
    assert found == []
