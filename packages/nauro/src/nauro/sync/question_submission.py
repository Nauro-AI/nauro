"""Explicit question submission and lookup-only recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from nauro.store.question_contract import (
    QuestionResult,
    QuestionScope,
    QuestionTransportError,
    verify_question_response,
)
from nauro.store.question_records import (
    QuestionSubmission,
    mark_question_uncertain,
    question_submission_lock,
    read_question_submission,
    record_question_result,
)
from nauro.store.submission_records import SubmissionRecordError, require_submission_actor


class QuestionRecoveryRequiredError(SubmissionRecordError):
    """The original question operation requires lookup before another send."""


class QuestionRetryExpiredError(SubmissionRecordError):
    """The original question operation is outside its resend horizon."""


class QuestionTransport(Protocol):
    def submit(self, record: QuestionSubmission) -> QuestionResult: ...

    def lookup(self, record: QuestionSubmission) -> QuestionResult: ...


def _load(scope: QuestionScope) -> QuestionSubmission:
    record = read_question_submission(scope)
    if record is None:
        raise SubmissionRecordError("The saved question submission is missing.")
    return record


def _require_window(record: QuestionSubmission) -> None:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    elapsed = datetime.now(timezone.utc) - created
    if not timedelta(0) <= elapsed <= timedelta(hours=24):
        raise QuestionRetryExpiredError("The question submission is outside the resend horizon.")


def _accept(record: QuestionSubmission, result: QuestionResult, *, lookup: bool) -> QuestionResult:
    result = verify_question_response(
        result.model_dump_json().encode(), record.scope, record.payload_json, lookup=lookup
    )
    require_submission_actor(record.scope.user_id)
    record_question_result(record, result)
    require_submission_actor(record.scope.user_id)
    return result


def _send(record: QuestionSubmission, transport: QuestionTransport) -> QuestionResult:
    _require_window(record)
    uncertain = mark_question_uncertain(record)
    require_submission_actor(record.scope.user_id)
    _require_window(uncertain)
    result = transport.submit(uncertain)
    if result.status == "absent":
        raise QuestionTransportError("A question send cannot return an absent lookup result.")
    return _accept(uncertain, result, lookup=False)


def submit_question(scope: QuestionScope, transport: QuestionTransport) -> QuestionResult:
    require_submission_actor(scope.user_id)
    with question_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        if record.phase != "prepared":
            raise QuestionRecoveryRequiredError(
                "Look up the original question operation before retrying."
            )
        return _send(record, transport)


def recover_question(scope: QuestionScope, transport: QuestionTransport) -> QuestionResult:
    require_submission_actor(scope.user_id)
    with question_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        return _accept(record, transport.lookup(record), lookup=True)


def retry_question(scope: QuestionScope, transport: QuestionTransport) -> QuestionResult:
    require_submission_actor(scope.user_id)
    with question_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        result = _accept(record, transport.lookup(record), lookup=True)
        if result.status != "absent":
            return result
        return _send(_load(scope), transport)
