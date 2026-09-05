"""Local submission durability and recovery across process boundaries."""

from __future__ import annotations

import errno
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from nauro_core.operations.commit_plan import canonical_judgment_payload_bytes

from nauro.store import submission_records as records
from nauro.sync import judgment_submission as submission

PROJECT = "01K00000000000000000000001"
USER = "01K00000000000000000000002"
OTHER = "01K00000000000000000000003"


def _payload():
    return canonical_judgment_payload_bytes(
        {
            "payload_schema": "nauro.judgment_commit.pre_team.v1",
            "approval_mode": "pre_team_session",
            "base_generation_id": "01K00000000000000000000000",
            "base_decision_counter": 4,
            "proposed_base_commit": None,
            "content": {
                "operation": "add",
                "affected_decision_id": None,
                "title": "Durable identity",
                "rationale": "Retain one identity and exact bytes across process restart.",
                "rejected": [],
                "confidence": "high",
                "decision_type": None,
                "reversibility": None,
                "files_affected": [],
                "resolves_questions": [],
            },
        }
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "client"
    root.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(root))
    (root / "config.json").write_text(
        json.dumps({"auth": {"user_id": USER, "access_token": "synthetic-test-token"}})
    )
    return root


def _prepared():
    return records.prepare_submission(PROJECT, USER, _payload())


def _result(record, status="committed"):
    return records.JudgmentTransportResult(
        scope=record.scope,
        payload_digest=record.payload_digest,
        status=status,
        receipt_json='{"receipt_id":"synthetic-receipt"}' if status == "committed" else None,
    )


def test_record_is_private_and_outside_sync_root(home):
    record = _prepared()
    path = records._record_path(record.scope)
    assert path.is_relative_to(home / "submission-records")
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (home / "projects").exists()
    assert records.read_submission(record.scope) == record
    assert records.list_submissions(PROJECT, USER) == (record,)
    assert record.payload_bytes == _payload()


def test_new_approval_gets_distinct_identity_for_identical_bytes(home):
    first, second = _prepared(), _prepared()
    assert first.scope.operation_id != second.scope.operation_id
    assert first.payload_digest == second.payload_digest
    assert len(records.list_submissions(PROJECT, USER)) == 2


def test_send_observes_durable_uncertain_state(home, monkeypatch):
    record = _prepared()
    events = []
    real_sync = records.os.fsync

    def fsync(fd):
        real_sync(fd)
        events.append("sync")

    def send(saved):
        assert saved.phase == "uncertain"
        assert records.read_submission(record.scope) == saved
        assert events[-2:] == ["sync", "sync"]
        events.append("send")
        return _result(saved)

    monkeypatch.setattr(records.os, "fsync", fsync)
    transport = Mock(submit=Mock(side_effect=send))
    result = submission.submit_judgment(record.scope, transport)
    assert result == _result(record)
    assert records.read_submission(record.scope).phase == "resolved"
    assert events[-2:] == ["sync", "sync"]
    assert events.count("send") == 1


def test_fsync_failure_prevents_send_and_preserves_record(home, monkeypatch):
    record = _prepared()
    transport = Mock()

    real_sync = records.os.fsync

    def fail(fd):
        if stat.S_ISREG(records.os.fstat(fd).st_mode):
            raise OSError(errno.ENOSPC, "synthetic disk full")
        real_sync(fd)

    monkeypatch.setattr(records.os, "fsync", fail)
    with pytest.raises(OSError) as error:
        submission.submit_judgment(record.scope, transport)
    assert error.value.errno == errno.ENOSPC
    transport.submit.assert_not_called()
    assert records.read_submission(record.scope) == record


def test_directory_sync_failure_after_replace_cannot_trigger_send(home, monkeypatch):
    record = _prepared()
    real_write = records.os.replace

    def replaced_then_fail(source, target):
        real_write(source, target)
        monkeypatch.setattr(
            records, "_directory_sync", Mock(side_effect=OSError(errno.EIO, "sync"))
        )

    monkeypatch.setattr(records.os, "replace", replaced_then_fail)
    transport = Mock()
    with pytest.raises(OSError) as error:
        submission.submit_judgment(record.scope, transport)
    assert error.value.errno == errno.EIO
    assert records.read_submission(record.scope).phase == "uncertain"
    transport.submit.assert_not_called()


def test_dropped_response_requires_lookup_and_replays_exact_receipt(home):
    record = _prepared()
    transport = Mock(submit=Mock(side_effect=ConnectionError("lost response")))
    with pytest.raises(ConnectionError):
        submission.submit_judgment(record.scope, transport)
    with pytest.raises(submission.SubmissionRecoveryRequiredError):
        submission.submit_judgment(record.scope, transport)
    assert transport.submit.call_count == 1
    transport.lookup.return_value = _result(record)
    assert submission.recover_judgment(record.scope, transport) == _result(record)
    transport.lookup.assert_called_once_with(record.scope, record.payload_digest)
    assert submission.submit_judgment(record.scope, transport) == _result(record)
    assert transport.submit.call_count == 1


@pytest.mark.parametrize("status", ["absent", "pending"])
def test_nonterminal_lookup_never_resends_or_resolves(home, status):
    record = _prepared()
    transport = Mock(lookup=Mock(return_value=_result(record, status)))
    assert submission.recover_judgment(record.scope, transport).status == status
    assert records.read_submission(record.scope).phase == (
        "uncertain" if status == "pending" else "prepared"
    )
    transport.submit.assert_not_called()


def test_explicit_retry_looks_up_before_resending_original_bytes(home):
    record = _prepared()
    transport = Mock()
    transport.lookup.return_value = _result(record, "absent")
    transport.submit.return_value = _result(record)
    assert submission.retry_judgment(record.scope, transport) == _result(record)
    assert [call[0] for call in transport.mock_calls] == ["lookup", "submit"]
    sent = transport.submit.call_args.args[0]
    assert sent.scope == record.scope
    assert sent.payload_bytes == record.payload_bytes


@pytest.mark.parametrize("status", ["stale", "expired", "disposed", "conflict", "pending"])
def test_retry_does_not_send_after_non_absent_lookup(home, status):
    record = _prepared()
    transport = Mock(lookup=Mock(return_value=_result(record, status)))
    assert submission.retry_judgment(record.scope, transport).status == status
    transport.submit.assert_not_called()


def test_expired_item_can_recover_but_cannot_resend(home):
    record = _prepared()
    expired = records.JudgmentSubmission.model_validate(
        {
            **record.model_dump(),
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
        }
    )
    records._write(expired)
    transport = Mock(lookup=Mock(return_value=_result(record, "absent")))
    with pytest.raises(submission.SubmissionRetryExpiredError):
        submission.retry_judgment(record.scope, transport)
    transport.submit.assert_not_called()
    transport.lookup.return_value = _result(record)
    assert submission.recover_judgment(record.scope, transport) == _result(record)


def test_actor_change_blocks_read_listing_and_send(home):
    record = _prepared()
    (home / "config.json").write_text(
        json.dumps({"auth": {"user_id": OTHER, "access_token": "synthetic-test-token"}})
    )
    transport = Mock()
    for action in (
        lambda: records.read_submission(record.scope),
        lambda: records.list_submissions(PROJECT, USER),
        lambda: submission.submit_judgment(record.scope, transport),
        lambda: records.prepare_submission(PROJECT, USER, _payload()),
    ):
        with pytest.raises(records.SubmissionActorMismatchError):
            action()
    transport.submit.assert_not_called()


def test_cross_bound_result_is_not_recorded(home):
    record = _prepared()
    wrong = _result(record).model_copy(update={"payload_digest": "0" * 64})
    transport = Mock(submit=Mock(return_value=wrong))
    with pytest.raises(submission.SubmissionProtocolError):
        submission.submit_judgment(record.scope, transport)
    assert records.read_submission(record.scope).phase == "uncertain"


@pytest.mark.parametrize("damage", ["digest", "duplicate", "truncated", "scope"])
def test_corrupt_record_is_not_missing_or_sent(home, damage):
    record = _prepared()
    path = records._record_path(record.scope)
    encoded = path.read_bytes()
    if damage == "duplicate":
        encoded = b'{"phase":"prepared",' + encoded[1:]
    elif damage == "truncated":
        encoded = encoded[:-1]
    else:
        raw = json.loads(encoded)
        if damage == "digest":
            raw["record"]["payload_digest"] = "0" * 64
        else:
            raw["record"]["scope"]["project_id"] = OTHER
        encoded = json.dumps(raw, separators=(",", ":")).encode()
    path.write_bytes(encoded)
    transport = Mock()
    with pytest.raises(records.SubmissionRecordCorruptError):
        submission.submit_judgment(record.scope, transport)
    transport.submit.assert_not_called()
    assert path.read_bytes() == encoded


def test_per_item_lock_excludes_another_process(home):
    record = _prepared()
    program = """
import sys
from nauro.store.submission_records import SubmissionScope, submission_lock
from filelock import Timeout
scope = SubmissionScope.model_validate_json(sys.argv[1])
try:
    with submission_lock(scope):
        raise SystemExit(2)
except Timeout:
    raise SystemExit(7)
"""
    with records.submission_lock(record.scope):
        child = subprocess.run(
            [sys.executable, "-c", program, record.scope.model_dump_json()],
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert child.returncode == 7, child.stderr


def test_restart_discovers_original_identity_after_process_exit_during_send(home):
    program = """
import os, sys
from nauro.store.submission_records import prepare_submission
from nauro.sync.judgment_submission import submit_judgment
record = prepare_submission(sys.argv[1], sys.argv[2], bytes.fromhex(sys.argv[3]))
class DroppedResponse:
    def submit(self, saved):
        os._exit(17)
submit_judgment(record.scope, DroppedResponse())
"""
    first = subprocess.run(
        [sys.executable, "-c", program, PROJECT, USER, _payload().hex()],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 17, first.stderr
    (saved,) = records.list_submissions(PROJECT, USER)
    assert saved.phase == "uncertain"
    assert saved.payload_bytes == _payload()
    transport = Mock(lookup=Mock(return_value=_result(saved)))
    assert submission.recover_judgment(saved.scope, transport) == _result(saved)
    transport.submit.assert_not_called()


def test_receipt_corruption_is_not_replayed_from_local_storage(home):
    record = _prepared()
    transport = Mock(submit=Mock(return_value=_result(record)))
    submission.submit_judgment(record.scope, transport)
    path = records._record_path(record.scope)
    raw = json.loads(path.read_bytes())
    raw["record"]["result"]["receipt_json"] = '{"receipt_id":"wrong-receipt"}'
    path.write_text(json.dumps(raw, separators=(",", ":")))
    with pytest.raises(records.SubmissionRecordCorruptError):
        submission.recover_judgment(record.scope, transport)
    transport.lookup.assert_not_called()


def test_permission_failure_is_not_absence(home, monkeypatch):
    record = _prepared()
    original_open = records.os.open
    target = records._record_path(record.scope)

    def denied(path, flags, *args, **kwargs):
        if path == target:
            raise PermissionError(errno.EACCES, "synthetic access refusal")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(records.os, "open", denied)
    transport = Mock()
    with pytest.raises(PermissionError) as error:
        submission.recover_judgment(record.scope, transport)
    assert error.value.errno == errno.EACCES
    transport.lookup.assert_not_called()


def test_failed_result_persistence_keeps_recovery_possible(home, monkeypatch):
    record = _prepared()
    transport = Mock(submit=Mock(return_value=_result(record)))
    write = records._write

    def fail_completion(updated):
        if updated.phase == "resolved":
            raise OSError(errno.ENOSPC, "synthetic full disk")
        write(updated)

    monkeypatch.setattr(records, "_write", fail_completion)
    with pytest.raises(OSError) as error:
        submission.submit_judgment(record.scope, transport)
    assert error.value.errno == errno.ENOSPC
    assert records.read_submission(record.scope).phase == "uncertain"
    monkeypatch.setattr(records, "_write", write)
    transport.lookup.return_value = _result(record)
    assert submission.recover_judgment(record.scope, transport) == _result(record)
    assert transport.submit.call_count == 1


def test_stale_local_observation_cannot_reset_a_resolved_item(home):
    record = _prepared()
    transport = Mock(submit=Mock(return_value=_result(record)))
    submission.submit_judgment(record.scope, transport)
    with records.submission_lock(record.scope):
        with pytest.raises(records.SubmissionRecordError, match="changed"):
            records.mark_uncertain(record)
    assert records.read_submission(record.scope).phase == "resolved"


def test_soft_lock_is_refused_before_send(home, monkeypatch):
    from filelock import SoftFileLock

    record = _prepared()
    monkeypatch.setattr(records, "FileLock", SoftFileLock)
    transport = Mock()
    with pytest.raises(records.SubmissionRecordError, match="Native submission locking"):
        submission.submit_judgment(record.scope, transport)
    transport.submit.assert_not_called()


def test_missing_record_never_becomes_a_new_submission(home):
    scope = records.SubmissionScope(project_id=PROJECT, user_id=USER, operation_id="missing")
    assert records.read_submission(scope) is None
    transport = Mock()
    with pytest.raises(records.SubmissionRecordError, match="missing"):
        submission.recover_judgment(scope, transport)
    transport.lookup.assert_not_called()
    transport.submit.assert_not_called()


def test_noncanonical_approval_cannot_create_a_record(home):
    with pytest.raises(ValueError):
        records.prepare_submission(PROJECT, USER, _payload() + b"\n")
    assert records.list_submissions(PROJECT, USER) == ()


def test_submission_coordinator_has_no_production_caller():
    import ast
    from pathlib import Path

    root = Path(submission.__file__).parents[1]
    callers = []
    for path in root.rglob("*.py"):
        if path == Path(submission.__file__):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.sync.judgment_submission":
                callers.append(path.relative_to(root).as_posix())
    assert callers == []
