"""``get_raw_file`` — return the raw text of any file in the project store.

Cross-transport implementation: every transport adapter calls this
function with the same arguments and receives the same
:class:`GetRawFileResult`. Each adapter wraps the call to add transport-
specific framing (``store`` field, adapter-side path-traversal guards,
and ``available_files`` hints); the file lookup
itself is shared by construction.

The kernel stays storage-agnostic: it does not inspect ``path`` for
traversal or backend-specific conventions. Adapters that need to reject
escapes (e.g. ``FilesystemStore``) own that concern at their boundary
before they hand the call off here.
"""

from __future__ import annotations

from nauro_core.operations.results import ErrorPayload, GetRawFileResult
from nauro_core.operations.store import Store


def get_raw_file(store: Store, path: str) -> GetRawFileResult:
    """Return the text body at store-relative ``path``.

    On a miss ``content`` stays unset and ``error`` names the requested path.
    """
    body = store.read_file(path)
    if body is None:
        return GetRawFileResult(
            error=ErrorPayload(kind="error", reason=f"File not found: {path}"),
        )
    return GetRawFileResult(content=body)
