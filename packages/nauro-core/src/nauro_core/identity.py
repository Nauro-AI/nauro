"""Identity helpers for deriving per-user storage keys.

Pure, zero-I/O string logic. ``nauro-core`` is compute-only, so this module
uses only the standard library.
"""

from __future__ import annotations

import string

_ALLOWED: frozenset[str] = frozenset(string.ascii_letters + string.digits + "_-")


def sanitize_sub(sub: str) -> str:
    """Sanitize an Auth0 ``sub`` claim into a per-user S3 key prefix.

    Contract:

    - Allowed characters are the ASCII class ``A-Z a-z 0-9 _ -``; they pass
      through unchanged.
    - Every other character is replaced one-for-one by a single ``-``. Runs are
      never collapsed: ``"a||b"`` becomes ``"a--b"``, not ``"a-b"``.
    - The result is truncated to the first 128 characters, applied after
      substitution.

    This is the single source of truth for deriving the per-user S3 key prefix
    across every surface that touches the store. The prefix is the storage
    isolation boundary, so any divergence would route a user's writes and reads
    to different prefixes. Do not let copies of this logic drift.
    """
    return "".join(c if c in _ALLOWED else "-" for c in sub)[:128]
