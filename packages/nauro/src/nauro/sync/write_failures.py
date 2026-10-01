"""Actionable diagnostics for saved mutation attempts."""

import httpx

from nauro.store.question_contract import QuestionTransportError
from nauro.store.state_contract import StateTransportError
from nauro.store.submission_records import (
    SubmissionActorMismatchError,
    SubmissionRecordCorruptError,
    SubmissionRecordError,
)
from nauro.sync.generation_session import GenerationConnectionError
from nauro.sync.question_submission import QuestionRecoveryRequiredError, QuestionRetryExpiredError
from nauro.sync.state_submission import StateRecoveryRequiredError, StateRetryExpiredError


def write_failure(error: Exception) -> dict[str, object]:
    if isinstance(error, (StateRetryExpiredError, QuestionRetryExpiredError)):
        status, code, guidance = (
            "retry_expired",
            "retry_horizon_expired",
            "The original 24-hour retry window has expired. Do not resend this attempt. "
            "Reconcile its outcome before creating a new write.",
        )
    elif isinstance(error, (StateRecoveryRequiredError, QuestionRecoveryRequiredError)):
        status, code, guidance = (
            "recovery_required",
            "lookup_required",
            "Use recover with this saved reference before retrying.",
        )
    elif isinstance(error, (GenerationConnectionError, SubmissionActorMismatchError)):
        status, code, guidance = (
            "blocked",
            "submission_authority_unavailable",
            "Restore the original account and project connection, then recover this reference.",
        )
    elif isinstance(error, SubmissionRecordCorruptError):
        status, code, guidance = (
            "blocked",
            "submission_record_invalid",
            "Preserve the saved record and repair its local storage. "
            "Reconcile the original operation before creating a new write.",
        )
    elif isinstance(error, httpx.HTTPError) or isinstance(error.__cause__, httpx.HTTPError):
        status, code, guidance = (
            "unresolved",
            "transport_outcome_unknown",
            "Recover this saved operation before retrying.",
        )
    elif isinstance(error, (StateTransportError, QuestionTransportError)):
        status, code, guidance = (
            "unverified",
            "response_unverified",
            "The response could not be verified. "
            "Recover this saved reference before another write.",
        )
    elif isinstance(error, (SubmissionRecordError, OSError)):
        status, code, guidance = (
            "blocked",
            "submission_record_unavailable",
            "Restore access to the saved record, then recover this reference before another write.",
        )
    else:
        status, code, guidance = (
            "unverified",
            "write_outcome_unverified",
            "Check the project connection and saved record, then recover this reference. "
            "Do not create a replacement write until the outcome is reconciled.",
        )
    return {"status": status, "error_code": code, "unresolved": True, "guidance": guidance}
