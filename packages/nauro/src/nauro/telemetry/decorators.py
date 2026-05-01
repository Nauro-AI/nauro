"""Decorators for telemetry instrumentation.

``mcp_tool(name)`` decorates each canonical MCP tool implementation in
``packages/nauro/src/nauro/mcp/tools.py`` so that one event fires per call,
regardless of which transport (stdio, local HTTP, or Lambda) routed in.

The transport label is read from ``current_transport()`` at emit time — never
hardcoded — because the same decorated function services all transports.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

from nauro.telemetry import capture
from nauro.telemetry._buckets import bucket
from nauro.telemetry.events import mcp_tool_called
from nauro.telemetry.transport import current_transport


def mcp_tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap an MCP tool to emit ``mcp.tool_called`` exactly once per call.

    Properties (D117 closed allowlist): tool_name, transport, success,
    duration_bucket. Never tool args, return values, or exception details.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            success = True
            try:
                return func(*args, **kwargs)
            except BaseException:
                success = False
                raise
            finally:
                try:
                    capture(
                        "mcp.tool_called",
                        mcp_tool_called(
                            tool_name=name,
                            transport=current_transport(),
                            success=success,
                            duration_bucket=bucket(time.perf_counter() - start),
                        ),
                    )
                except Exception:
                    pass

        return wrapper

    return decorator
