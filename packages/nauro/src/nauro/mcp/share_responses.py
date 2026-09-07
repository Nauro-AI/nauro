"""Dormant MCP responses for explicit saved share operations."""

from __future__ import annotations

from collections.abc import Callable

from filelock import Timeout
from mcp.types import CallToolResult, TextContent

from nauro.auth import ActiveUserReadError
from nauro.store.share_contract import ShareCommitted, ShareResult, ShareScope
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import share_submission
from nauro.sync.share_submission import ShareTransport

_RESULT_TEXT = {
    "absent": (
        "No receipt was found. This operation remains unresolved; an earlier request may "
        "still commit. Recovery only checks for a receipt."
    ),
    "slug_conflict_observed": (
        "This slug is already in use. This operation remains "
        "unresolved; an earlier request may still commit. Recover the original operation. "
        "Changed slug, content, pointer kind or summary requires a new explicit submission."
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


def _response(result: ShareResult) -> CallToolResult:
    if isinstance(result, ShareCommitted):
        text = "Share operation committed. This receipt does not establish current local state."
    else:
        text = _RESULT_TEXT[result.status]
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=result.model_dump(mode="json"),
        isError=result.status != "committed",
    )


def _run(operation: Callable[[], ShareResult]) -> CallToolResult:
    try:
        return _response(operation())
    except (SubmissionRecordError, ActiveUserReadError, OSError, Timeout):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        "Share operation outcome is unconfirmed. Keep the saved submission "
                        "and recover the original operation when access is available. "
                        "Do not infer that no write occurred."
                    ),
                )
            ],
            isError=True,
        )


def submit_share(scope: ShareScope, transport: ShareTransport) -> CallToolResult:
    return _run(lambda: share_submission.submit_share(scope, transport))


def recover_share(scope: ShareScope, transport: ShareTransport) -> CallToolResult:
    return _run(lambda: share_submission.recover_share(scope, transport))


def retry_share(scope: ShareScope, transport: ShareTransport) -> CallToolResult:
    return _run(lambda: share_submission.retry_share(scope, transport))
