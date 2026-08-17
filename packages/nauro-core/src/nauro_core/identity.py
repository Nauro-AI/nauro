"""Identity helpers for deriving per-user storage keys.

Pure, zero-I/O string logic. ``nauro-core`` is compute-only, so this module
uses only the standard library.
"""

from __future__ import annotations

import string

_ALLOWED: frozenset[str] = frozenset(string.ascii_letters + string.digits + "_-")


def sanitize_sub(sub: str) -> str:
    """Sanitize an Auth0 ``sub`` into a per-user S3 key prefix: each character outside
    ``A-Za-z0-9_-`` becomes one ``-``, runs never collapsed, then truncate to 128.
    The prefix is the storage isolation boundary; this is its only definition.
    """
    return "".join(c if c in _ALLOWED else "-" for c in sub)[:128]
