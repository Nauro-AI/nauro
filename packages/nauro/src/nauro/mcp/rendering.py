"""Shared render-or-fallback step for read-tool envelopes.

Both local text surfaces — the stdio MCP server's ``content[0]`` block and
the CLI's ``--format text`` mode — render an envelope through
``nauro_core.renderers.RENDERERS`` with the same failure contract: a tool
with no renderer mapped, or a renderer that raises, must never swallow the
response. This module owns that step so the two surfaces cannot drift.

Diagnostics stay with the caller: the stdio server logs a renderer failure
to its server log, while the CLI falls back silently with json-mode
streams — a traceback on the CLI's stderr would break the documented
stream contract. The outcome therefore carries the failure instead of
logging it here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from nauro_core.renderers import RENDERERS

from nauro.store.filesystem_store import canonical_store_relative_path


class RenderOutcome(NamedTuple):
    """Result of one render attempt.

    ``text`` is ``None`` when the caller must fall back to its own JSON emission
    of the envelope. ``failure`` carries the renderer's exception, or ``None``
    when no renderer was registered.
    """

    text: str | None
    failure: Exception | None


def resolve_renderer_kwargs(
    tool_name: str, request_args: dict[str, Any], store_path: Path | None
) -> dict[str, Any]:
    """Return the renderer kwargs for one read tool, shared by the stdio server and CLI text mode.

    An unknown tool or an unusable argument omits the kwarg rather than raising.
    """
    if tool_name == "get_decision":
        mode = request_args.get("mode")
        return {"mode": mode} if isinstance(mode, str) else {}
    if tool_name == "search_decisions":
        query = request_args.get("query")
        return {"query": query} if isinstance(query, str) else {}
    if tool_name == "get_context":
        level = request_args.get("level")
        return {"level": level} if isinstance(level, (str, int)) else {}
    if tool_name == "get_raw_file":
        raw_path = request_args.get("path")
        if store_path is None or not isinstance(raw_path, str):
            return {}
        canonical = canonical_store_relative_path(store_path, raw_path)
        return {"path": canonical} if canonical is not None else {}
    return {}


def try_render_envelope(
    tool_name: str, envelope: dict, renderer_kwargs: dict[str, Any] | None = None
) -> RenderOutcome:
    """Render ``envelope`` through the tool's registered renderer.

    ``renderer_kwargs`` threads renderer-specific options without storing them on the envelope.
    """
    renderer = RENDERERS.get(tool_name)
    if renderer is None:
        return RenderOutcome(None, None)
    try:
        return RenderOutcome(renderer(envelope, **(renderer_kwargs or {})), None)
    except Exception as exc:
        return RenderOutcome(None, exc)
