"""Shared plumbing for plan-returning kernel operations.

Two operation conventions live in this package. Store-writing operations
execute their own reads and writes through the ``Store`` protocol.
Plan-returning operations (``update_stack``, ``share_context``,
``submit_report``, and the hosted plan path of ``update_state``, which
stays Store-writing locally) write nothing: the kernel validates the
payload, deterministically derives every artifact path, digest, and
precondition, and returns a frozen plan; the hosted server owns
observation, claims, and storage execution. The kernel's concurrency
vocabulary is content digests and the absent token, never storage-layer
tokens such as S3 ETags.

This module carries the shared typed Tier-1 rejection and the canonical
payload-byte encoding that single-sources the idempotency digest input.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


class PlanRejected(ValueError):
    """A plan-returning operation refused its payload at Tier-1 validation.

    ``reason`` is the caller-facing rejection text; no plan exists and
    nothing may be written when this raises.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def canonical_payload_bytes(payload: Mapping[str, object]) -> bytes:
    """Encode *payload* as canonical JSON bytes.

    Sorted keys, compact separators, UTF-8 unescaped, so the same payload always digests alike.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
