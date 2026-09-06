"""Dormant MCP responses for explicit saved question operations."""

from __future__ import annotations

from collections.abc import Callable

from filelock import Timeout
from mcp.types import CallToolResult, TextContent

from nauro.auth import ActiveUserReadError
from nauro.store.question_contract import QuestionResult, QuestionScope
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import question_submission
from nauro.sync.question_submission import QuestionTransport

_RESULT_TEXT = {
    "absent": (
        "No receipt was found. This operation remains unresolved; an earlier request may "
        "still commit. Recovery only checks for a receipt."
    ),
    "no_change_observed": (
        "This attempt made no question resolution change. This operation remains "
        "unresolved; an earlier request may still commit. Recover the original operation."
    ),
    "expired": (
        "The receipt replay window has expired. This does not prove that no write occurred. "
        "Do not resend this operation."
    ),
    "digest_conflict": (
        "This operation identity is bound to a different payload. "
        "Do not resend this operation with changed content."
    ),
}


def _response(result: QuestionResult) -> CallToolResult:
    if result.status == "committed":
        text = "Question operation committed. This receipt does not establish current local state."
    else:
        text = _RESULT_TEXT[result.status]
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=result.model_dump(mode="json"),
        isError=result.status != "committed",
    )


def _run(operation: Callable[[], QuestionResult]) -> CallToolResult:
    try:
        return _response(operation())
    except (SubmissionRecordError, ActiveUserReadError, OSError, Timeout):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        "Question operation outcome is unconfirmed. Keep the saved submission "
                        "and recover the original operation when access is available. "
                        "Do not infer that no write occurred."
                    ),
                )
            ],
            isError=True,
        )


def submit_question(scope: QuestionScope, transport: QuestionTransport) -> CallToolResult:
    return _run(lambda: question_submission.submit_question(scope, transport))


def recover_question(scope: QuestionScope, transport: QuestionTransport) -> CallToolResult:
    return _run(lambda: question_submission.recover_question(scope, transport))


def retry_question(scope: QuestionScope, transport: QuestionTransport) -> CallToolResult:
    return _run(lambda: question_submission.retry_question(scope, transport))
