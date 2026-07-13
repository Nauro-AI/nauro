"""Internal payload builders used by AGENTS.md generation and the store validator.

The MCP ``get_context`` tool boundary returns the kernel's dict envelope
(see :func:`nauro.mcp.tools.tool_get_context`); these helpers stay string-
returning because their callers — AGENTS.md regen, L0 token-budget
validation — assemble their own surrounding markdown and only need the
context body.
"""

from pathlib import Path

from nauro_core.operations import get_context as _get_context_op

from nauro.store.filesystem_store import FilesystemStore


def _context_text(store_path: Path, level: int) -> str:
    result = _get_context_op(FilesystemStore(store_path), level)
    # Internal callers (AGENTS.md regen, validator) always pass a valid
    # level today, so the kernel rejection branch is unreachable. Assert
    # rather than swallow — surface any future drift loudly instead of
    # rendering an empty payload into a published markdown file.
    assert result.error is None, f"unexpected get_context error: {result.error}"
    return result.content or ""


def build_l0_payload(store_path: Path) -> str:
    """Build L0 payload (concise summary)."""
    return _context_text(store_path, 0)
