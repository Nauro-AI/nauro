"""Transport ContextVar for MCP tool emission.

Per CLAUDE.md, ``mcp/tools.py`` is the canonical implementation for stdio AND
HTTP transports — both ``stdio_server.py`` and the FastAPI ``server.py`` (plus
the muscat Lambda, in its own repo) delegate here. The ``transport`` field on
``mcp.tool_called`` therefore cannot be hardcoded in the decorator; each entry
point sets the value before dispatching.

Default ``"stdio"`` is a fail-soft: any future entry point that forgets to set
the value still emits, just attributed to stdio.
"""

from __future__ import annotations

from contextvars import ContextVar

_TRANSPORT: ContextVar[str] = ContextVar("nauro_telemetry_transport", default="stdio")


def set_transport(name: str) -> None:
    _TRANSPORT.set(name)


def current_transport() -> str:
    return _TRANSPORT.get()
