"""Resolve the HEAD commit of the repository enclosing a directory.

Provenance capture for the decision write path: the local transports stamp
the resolved SHA into new decision frontmatter. Resolution is fail-open —
no repository, no git binary, no commits, a bad cwd, or a timeout all
resolve to ``None`` and the stamp is simply omitted, never written as null.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from nauro_core.provenance import InvalidCommitSha, validate_commit_sha

# Bounds the capture cost on every decision write; a repository that cannot
# answer rev-parse within this window resolves to absence.
_GIT_TIMEOUT_SECONDS = 2


def resolve_repo_head(cwd: str | Path | None) -> str | None:
    """Return the HEAD SHA of the innermost repository enclosing ``cwd``.

    Never raises: any resolution failure returns ``None``.
    """
    if cwd is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        # Never raises: beyond OSError and SubprocessError, subprocess.run
        # raises ValueError on a NUL byte in the client-supplied cwd and
        # text mode can raise UnicodeDecodeError; every failure is absence.
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    try:
        return validate_commit_sha(sha, field="HEAD")
    except InvalidCommitSha:
        return None


def resolve_process_repo_head() -> str | None:
    """Return the HEAD SHA of the repository enclosing the process cwd.

    Never raises: a deleted working directory resolves to absence, not an abort.
    """
    try:
        cwd = Path.cwd()
    except Exception:
        return None
    return resolve_repo_head(cwd)
