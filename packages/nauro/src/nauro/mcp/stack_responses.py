"""Dormant MCP responses for explicit saved stack operations."""

from __future__ import annotations

from collections.abc import Callable

from filelock import Timeout
from mcp.types import CallToolResult, TextContent

from nauro.auth import ActiveUserReadError
from nauro.store.stack_contract import StackCommitted, StackResult, StackScope
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import stack_submission
from nauro.sync.stack_submission import StackTransport

_RESULT_TEXT = {
    "absent": (
        "No receipt was found. This operation remains unresolved; an earlier request may "
        "still commit. Recovery only checks for a receipt."
    ),
    "revision_conflict_observed": (
        "This attempt found a revision conflict and made no write. This operation remains "
        "unresolved; an earlier request may still commit. Recover the original operation. "
        "Changed content or expected revision requires a new explicit submission."
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


def _response(result: StackResult) -> CallToolResult:
    if isinstance(result, StackCommitted):
        text = "Stack operation committed. This receipt does not establish current local state."
    else:
        text = _RESULT_TEXT[result.status]
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=result.model_dump(mode="json"),
        isError=result.status != "committed",
    )


def _run(operation: Callable[[], StackResult]) -> CallToolResult:
    try:
        return _response(operation())
    except (SubmissionRecordError, ActiveUserReadError, OSError, Timeout):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        "Stack operation outcome is unconfirmed. Keep the saved submission "
                        "and recover the original operation when access is available. "
                        "Do not infer that no write occurred."
                    ),
                )
            ],
            isError=True,
        )


def submit_stack(scope: StackScope, transport: StackTransport) -> CallToolResult:
    return _run(lambda: stack_submission.submit_stack(scope, transport))


def recover_stack(scope: StackScope, transport: StackTransport) -> CallToolResult:
    return _run(lambda: stack_submission.recover_stack(scope, transport))


def retry_stack(scope: StackScope, transport: StackTransport) -> CallToolResult:
    return _run(lambda: stack_submission.retry_stack(scope, transport))
