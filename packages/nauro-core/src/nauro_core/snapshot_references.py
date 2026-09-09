"""Canonical archive descriptors over retained immutable file bytes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from nauro_core.protected_generation_membership import is_hosted_snapshot_content_member


def snapshot_descriptor(files: Mapping[str, bytes], *, timestamp: str, trigger: str) -> bytes:
    return json.dumps(
        {
            "schema": "nauro.snapshot.references.v1",
            "timestamp": timestamp,
            "trigger": trigger,
            "files": {
                path: {"sha256": hashlib.sha256(body).hexdigest(), "length": len(body)}
                for path, body in sorted(files.items())
                if is_hosted_snapshot_content_member(path)
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
