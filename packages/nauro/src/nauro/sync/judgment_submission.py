"""Dormant send and lookup coordination for durable local judgment identities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from nauro.store.submission_records import (
    JudgmentSubmission,
    JudgmentTransportResult,
    SubmissionRecordError,
    SubmissionScope,
    mark_uncertain,
    read_submission,
    record_result,
    require_submission_actor,
    submission_lock,
)


class JudgmentTransport(Protocol):
    """Authenticate the saved actor and verify remote evidence before returning a result."""

    def submit(self, record: JudgmentSubmission) -> JudgmentTransportResult: ...

    def lookup(self, scope: SubmissionScope, payload_digest: str) -> JudgmentTransportResult: ...


class SubmissionRecoveryRequiredError(SubmissionRecordError):
    """An uncertain operation requires lookup before another send."""


class SubmissionRetryExpiredError(SubmissionRecordError):
    """The original operation is outside its permitted resend horizon."""


class SubmissionProtocolError(SubmissionRecordError):
    """Transport evidence does not bind the saved operation."""


def _load(scope: SubmissionScope) -> JudgmentSubmission:
    require_submission_actor(scope.user_id)
    record = read_submission(scope)
    if record is None:
        raise SubmissionRecordError("The saved submission is missing.")
    require_submission_actor(scope.user_id)
    return record


def _require_retry_window(record: JudgmentSubmission) -> None:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    elapsed = datetime.now(timezone.utc) - created
    if not timedelta(0) <= elapsed <= timedelta(hours=24):
        raise SubmissionRetryExpiredError("The original submission is outside the retry horizon.")


def _accept(record: JudgmentSubmission, result: JudgmentTransportResult) -> JudgmentTransportResult:
    result = JudgmentTransportResult.model_validate(result)
    if result.scope != record.scope or result.payload_digest != record.payload_digest:
        raise SubmissionProtocolError("The transport result does not bind the saved submission.")
    if result.status == "pending" and record.phase == "prepared":
        mark_uncertain(record)
    if result.status not in {"absent", "pending"}:
        record_result(record, result)
    require_submission_actor(record.scope.user_id)
    return result


def _send(record: JudgmentSubmission, transport: JudgmentTransport) -> JudgmentTransportResult:
    _require_retry_window(record)
    uncertain = mark_uncertain(record)
    require_submission_actor(record.scope.user_id)
    _require_retry_window(record)
    result = transport.submit(uncertain)
    if result.status == "absent":
        raise SubmissionProtocolError("A send cannot return an absent lookup result.")
    return _accept(uncertain, result)


def submit_judgment(
    scope: SubmissionScope, transport: JudgmentTransport
) -> JudgmentTransportResult:
    """Send a prepared item once; uncertain sends require explicit recovery."""
    require_submission_actor(scope.user_id)
    with submission_lock(scope):
        record = _load(scope)
        if record.result is not None:
            return record.result
        if record.phase != "prepared":
            raise SubmissionRecoveryRequiredError("Resolve the original operation before retrying.")
        return _send(record, transport)


def recover_judgment(
    scope: SubmissionScope, transport: JudgmentTransport
) -> JudgmentTransportResult:
    """Look up the original operation without submitting any payload."""
    require_submission_actor(scope.user_id)
    with submission_lock(scope):
        record = _load(scope)
        if record.result is not None:
            return record.result
        result = transport.lookup(scope, record.payload_digest)
        return _accept(record, result)


def retry_judgment(scope: SubmissionScope, transport: JudgmentTransport) -> JudgmentTransportResult:
    """Resend identical bytes only after lookup proves absence and the horizon permits it."""
    require_submission_actor(scope.user_id)
    with submission_lock(scope):
        record = _load(scope)
        if record.result is not None:
            return record.result
        result = _accept(record, transport.lookup(scope, record.payload_digest))
        if result.status != "absent":
            return result
        return _send(record, transport)
