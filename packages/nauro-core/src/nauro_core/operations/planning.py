"""Shared plumbing for plan-returning kernel operations.

A second operation convention lives beside the Store-writing operations in
this package. Store-writing operations (``flag_question``, ``update_state``,
…) execute their own reads and writes through the ``Store`` protocol.
Plan-returning operations (``update_stack``, ``share_context``) write
nothing: the kernel validates the submitted payload, deterministically
derives every artifact path, digest, and precondition, and returns a frozen
plan model; the hosted server owns observation, claims, and storage
execution. The kernel's only concurrency vocabulary is content digests and
the absent token — storage-layer tokens such as S3 ETags never enter the
kernel.

This module carries the pieces both plan-returning operations share: the
typed Tier-1 rejection and the canonical payload-byte encoding that
single-sources the idempotency digest input.
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

    Sorted keys, compact separators, UTF-8 without ASCII escaping — the
    deterministic byte form every surface digests for idempotency scope
    resolution, so the same payload always produces the same digest.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
