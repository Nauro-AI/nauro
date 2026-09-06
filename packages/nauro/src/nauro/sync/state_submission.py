"""Explicit state submission and lookup-only recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from nauro.store.state_contract import (
    StateResult,
    StateScope,
    StateTransportError,
    verify_state_response,
)
from nauro.store.state_records import (
    StateSubmission,
    mark_state_uncertain,
    read_state_submission,
    record_state_result,
    state_submission_lock,
)
from nauro.store.submission_records import SubmissionRecordError, require_submission_actor


class StateRecoveryRequiredError(SubmissionRecordError):
    """The original state operation requires lookup before another send."""


class StateRetryExpiredError(SubmissionRecordError):
    """The original state operation is outside its resend horizon."""


class StateTransport(Protocol):
    def submit(self, record: StateSubmission) -> StateResult: ...

    def lookup(self, record: StateSubmission) -> StateResult: ...


def _load(scope: StateScope) -> StateSubmission:
    record = read_state_submission(scope)
    if record is None:
        raise SubmissionRecordError("The saved state submission is missing.")
    return record


def _require_window(record: StateSubmission) -> None:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    elapsed = datetime.now(timezone.utc) - created
    if not timedelta(0) <= elapsed <= timedelta(hours=24):
        raise StateRetryExpiredError("The state submission is outside the resend horizon.")


def _accept(record: StateSubmission, result: StateResult, *, lookup: bool) -> StateResult:
    result = verify_state_response(
        result.model_dump_json().encode(), record.scope, record.payload_json, lookup=lookup
    )
    require_submission_actor(record.scope.user_id)
    record_state_result(record, result)
    require_submission_actor(record.scope.user_id)
    return result


def _send(record: StateSubmission, transport: StateTransport) -> StateResult:
    _require_window(record)
    uncertain = mark_state_uncertain(record)
    require_submission_actor(record.scope.user_id)
    _require_window(uncertain)
    result = transport.submit(uncertain)
    if result.status == "absent":
        raise StateTransportError("A state send cannot return an absent lookup result.")
    return _accept(uncertain, result, lookup=False)


def submit_state(scope: StateScope, transport: StateTransport) -> StateResult:
    require_submission_actor(scope.user_id)
    with state_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        if record.phase != "prepared":
            raise StateRecoveryRequiredError(
                "Look up the original state operation before retrying."
            )
        return _send(record, transport)


def recover_state(scope: StateScope, transport: StateTransport) -> StateResult:
    require_submission_actor(scope.user_id)
    with state_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        return _accept(record, transport.lookup(record), lookup=True)


def retry_state(scope: StateScope, transport: StateTransport) -> StateResult:
    require_submission_actor(scope.user_id)
    with state_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        result = _accept(record, transport.lookup(record), lookup=True)
        if result.status != "absent":
            return result
        return _send(_load(scope), transport)
