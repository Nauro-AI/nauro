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

from typing import Any, NamedTuple

from nauro_core.renderers import RENDERERS


class RenderOutcome(NamedTuple):
    """Result of one render attempt.

    ``text`` is the rendered block, or ``None`` when the caller must fall
    back to its own JSON emission of the envelope. ``failure`` carries the
    renderer's exception when one raised (``None`` for the no-renderer
    case), so callers can attach their own diagnostics.
    """

    text: str | None
    failure: Exception | None


def try_render_envelope(
    tool_name: str, envelope: dict, renderer_kwargs: dict[str, Any] | None = None
) -> RenderOutcome:
    """Render ``envelope`` through the tool's registered renderer.

    ``renderer_kwargs`` threads renderer-specific options (e.g.
    ``get_decision``'s requested ``mode``) without storing them on the
    envelope.
    """
    renderer = RENDERERS.get(tool_name)
    if renderer is None:
        return RenderOutcome(None, None)
    try:
        return RenderOutcome(renderer(envelope, **(renderer_kwargs or {})), None)
    except Exception as exc:
        return RenderOutcome(None, exc)
