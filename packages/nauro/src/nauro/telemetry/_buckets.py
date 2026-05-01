"""Shared duration bucketing for D117 events.

Same scheme used by `cli.command_invoked` and `mcp.tool_called` so dashboards
can compare CLI vs MCP latency on the same axis.
"""

from __future__ import annotations

_DURATION_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.010, "<10ms"),
    (0.100, "10-100ms"),
    (1.000, "100ms-1s"),
    (10.000, "1-10s"),
)
_LARGEST_BUCKET = ">10s"


def bucket(elapsed: float) -> str:
    for threshold, label in _DURATION_BUCKETS:
        if elapsed < threshold:
            return label
    return _LARGEST_BUCKET
