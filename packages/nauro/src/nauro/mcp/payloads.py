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
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.read_authority import require_legacy_context
from nauro.sync.generation_guidance import generation_notice, read_generation_guidance


def _context_text(store_path: Path, level: int) -> str:
    require_legacy_context(store_path)
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


def build_guidance_payload(
    store_path: Path, *, snapshot: GenerationSnapshotStore | None = None
) -> tuple[str, str | None]:
    result = read_generation_guidance(
        store_path, lambda store: _get_context_op(store, 0), snapshot=snapshot
    )
    if result is None:
        return build_l0_payload(store_path), None
    context, identity = result
    if context.error is not None:
        raise PermissionError("Generation guidance could not be rendered.")
    return context.content or "", generation_notice(identity)
