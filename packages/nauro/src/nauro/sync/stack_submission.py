"""Explicit stack submission and lookup-only recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from nauro.store.stack_contract import (
    StackResult,
    StackScope,
    StackTransportError,
    verify_stack_response,
)
from nauro.store.stack_records import (
    StackSubmission,
    mark_stack_uncertain,
    read_stack_submission,
    record_stack_result,
    stack_submission_lock,
)
from nauro.store.submission_records import SubmissionRecordError, require_submission_actor


class StackRecoveryRequiredError(SubmissionRecordError):
    """The original stack operation requires lookup before another send."""


class StackRetryExpiredError(SubmissionRecordError):
    """The original stack operation is outside its resend horizon."""


class StackTransport(Protocol):
    def submit(self, record: StackSubmission) -> StackResult: ...

    def lookup(self, record: StackSubmission) -> StackResult: ...


def _load(scope: StackScope) -> StackSubmission:
    record = read_stack_submission(scope)
    if record is None:
        raise SubmissionRecordError("The saved stack submission is missing.")
    return record


def _require_window(record: StackSubmission) -> None:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    elapsed = datetime.now(timezone.utc) - created
    if not timedelta(0) <= elapsed <= timedelta(hours=24):
        raise StackRetryExpiredError("The stack submission is outside the resend horizon.")


def _accept(record: StackSubmission, result: StackResult, *, lookup: bool) -> StackResult:
    result = verify_stack_response(
        result.model_dump_json().encode(), record.scope, record.payload_json, lookup=lookup
    )
    require_submission_actor(record.scope.user_id)
    record_stack_result(record, result)
    require_submission_actor(record.scope.user_id)
    return result


def _send(record: StackSubmission, transport: StackTransport) -> StackResult:
    _require_window(record)
    uncertain = mark_stack_uncertain(record)
    require_submission_actor(record.scope.user_id)
    _require_window(uncertain)
    result = transport.submit(uncertain)
    if result.status == "absent":
        raise StackTransportError("A stack send cannot return an absent lookup result.")
    return _accept(uncertain, result, lookup=False)


def submit_stack(scope: StackScope, transport: StackTransport) -> StackResult:
    require_submission_actor(scope.user_id)
    with stack_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        if record.phase != "prepared":
            raise StackRecoveryRequiredError(
                "Look up the original stack operation before retrying."
            )
        return _send(record, transport)


def recover_stack(scope: StackScope, transport: StackTransport) -> StackResult:
    require_submission_actor(scope.user_id)
    with stack_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        return _accept(record, transport.lookup(record), lookup=True)


def retry_stack(scope: StackScope, transport: StackTransport) -> StackResult:
    require_submission_actor(scope.user_id)
    with stack_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        result = _accept(record, transport.lookup(record), lookup=True)
        if result.status != "absent":
            return result
        return _send(_load(scope), transport)
