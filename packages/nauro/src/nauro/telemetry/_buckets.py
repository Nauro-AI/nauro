"""Shared duration bucketing for telemetry events.

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


_BYTE_BUCKETS: tuple[tuple[int, str], ...] = (
    (10_000, "<10KB"),
    (100_000, "10-100KB"),
    (1_000_000, "100KB-1MB"),
    (10_000_000, "1-10MB"),
)
_LARGEST_BYTE_BUCKET = ">10MB"


def byte_bucket(size: int) -> str:
    """Bucket a byte count onto a coarse size axis for `sync.completed`.

    Same coarsening intent as `bucket()`: emit a privacy-preserving magnitude
    label, never the exact payload size.
    """
    for threshold, label in _BYTE_BUCKETS:
        if size < threshold:
            return label
    return _LARGEST_BYTE_BUCKET
