"""Explicit share submission and lookup-only recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from nauro.store.share_contract import (
    ShareResult,
    ShareScope,
    ShareTransportError,
    verify_share_response,
)
from nauro.store.share_records import (
    ShareSubmission,
    mark_share_uncertain,
    read_share_submission,
    record_share_result,
    share_submission_lock,
)
from nauro.store.submission_records import SubmissionRecordError, require_submission_actor


class ShareRecoveryRequiredError(SubmissionRecordError):
    """The original share operation requires lookup before another send."""


class ShareRetryExpiredError(SubmissionRecordError):
    """The original share operation is outside its resend horizon."""


class ShareTransport(Protocol):
    def submit(self, record: ShareSubmission) -> ShareResult: ...

    def lookup(self, record: ShareSubmission) -> ShareResult: ...


def _load(scope: ShareScope) -> ShareSubmission:
    record = read_share_submission(scope)
    if record is None:
        raise SubmissionRecordError("The saved share submission is missing.")
    return record


def _require_window(record: ShareSubmission) -> None:
    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
    elapsed = datetime.now(timezone.utc) - created
    if not timedelta(0) <= elapsed <= timedelta(hours=24):
        raise ShareRetryExpiredError("The share submission is outside the resend horizon.")


def _accept(record: ShareSubmission, result: ShareResult, *, lookup: bool) -> ShareResult:
    result = verify_share_response(
        result.model_dump_json().encode(), record.scope, record.payload_json, lookup=lookup
    )
    require_submission_actor(record.scope.user_id)
    record_share_result(record, result)
    require_submission_actor(record.scope.user_id)
    return result


def _send(record: ShareSubmission, transport: ShareTransport) -> ShareResult:
    _require_window(record)
    uncertain = mark_share_uncertain(record)
    require_submission_actor(record.scope.user_id)
    _require_window(uncertain)
    result = transport.submit(uncertain)
    if result.status == "absent":
        raise ShareTransportError("A share send cannot return an absent lookup result.")
    return _accept(uncertain, result, lookup=False)


def submit_share(scope: ShareScope, transport: ShareTransport) -> ShareResult:
    require_submission_actor(scope.user_id)
    with share_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        if record.phase != "prepared":
            raise ShareRecoveryRequiredError(
                "Look up the original share operation before retrying."
            )
        return _send(record, transport)


def recover_share(scope: ShareScope, transport: ShareTransport) -> ShareResult:
    require_submission_actor(scope.user_id)
    with share_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        return _accept(record, transport.lookup(record), lookup=True)


def retry_share(scope: ShareScope, transport: ShareTransport) -> ShareResult:
    require_submission_actor(scope.user_id)
    with share_submission_lock(scope):
        record = _load(scope)
        if record.result is not None and not record.result.unresolved:
            return record.result
        result = _accept(record, transport.lookup(record), lookup=True)
        if result.status != "absent":
            return result
        return _send(_load(scope), transport)
