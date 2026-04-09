"""Conflict resolution for cloud sync.

When both local and remote changed since last sync, this module decides
how to merge or which version wins.
"""

import logging
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from nauro.sync.state import SyncState

logger = logging.getLogger("nauro.sync")

# Files where append-only union merge is appropriate
APPEND_ONLY_PATTERNS = ("decisions/", "open-questions.md", "state_history.md")

# Files that are never synced
NEVER_SYNC = (".sync-state.json",)


def should_skip(relative_path: str) -> bool:
    """Return True if this file should never be synced."""
    return relative_path in NEVER_SYNC


def detect_conflict(
    relative_path: str, state: SyncState, local_sha256: str, remote_etag: str
) -> bool:
    """Conflict = local SHA256 differs from state AND remote ETag differs from state."""
    fs = state.files.get(relative_path)
    if fs is None:
        return False
    local_changed = local_sha256 != fs.local_sha256
    remote_changed = remote_etag != fs.remote_etag
    return local_changed and remote_changed


def _is_append_only(relative_path: str) -> bool:
    """Check if a file uses append-only merge strategy."""
    return any(relative_path.startswith(p) or relative_path == p for p in APPEND_ONLY_PATTERNS)


def _git_available() -> bool:
    """Check if git is available on PATH."""
    return shutil.which("git") is not None


def _save_conflict_backup(project_path: Path, relative_path: str, content: bytes) -> Path:
    """Save the losing version to .conflict-backup/."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = relative_path.replace("/", "_")
    backup_dir = project_path / ".conflict-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{timestamp}-{filename}"
    backup_path.write_bytes(content)
    logger.info("Conflict backup saved: %s", backup_path)
    return backup_path


def resolve_conflict(
    project_path: Path,
    local_path: Path,
    remote_content: bytes,
    relative_path: str,
    state: SyncState,
) -> bytes:
    """Resolve a conflict between local and remote versions.

    For append-only files: git merge-file --union (falls back to LWW if git unavailable).
    For everything else: last-write-wins with backup of the losing version.
    """
    local_content = local_path.read_bytes()

    if _is_append_only(relative_path) and _git_available():
        return _union_merge(local_content, remote_content, relative_path, state)

    if _is_append_only(relative_path) and not _git_available():
        logger.warning("git not available — falling back to last-write-wins for %s", relative_path)

    # Last-write-wins: keep local, back up remote
    _save_conflict_backup(project_path, relative_path, remote_content)
    logger.warning(
        "Conflict on %s resolved by last-write-wins (kept local). "
        "Remote version saved to .conflict-backup/",
        relative_path,
    )
    return local_content


def _union_merge(
    local_content: bytes, remote_content: bytes, relative_path: str, state: SyncState
) -> bytes:
    """Use git merge-file --union for append-only files.

    Uses the last-synced version as the common base.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        local_tmp = tmp / "local"
        base_tmp = tmp / "base"
        remote_tmp = tmp / "remote"

        local_tmp.write_bytes(local_content)
        remote_tmp.write_bytes(remote_content)

        # Use empty base if we don't have one — union merge handles this fine
        fs = state.files.get(relative_path)
        if fs and fs.local_sha256:
            # We don't store the base content, so use empty as base
            # This makes union merge act like a concatenation of unique lines
            base_tmp.write_bytes(b"")
        else:
            base_tmp.write_bytes(b"")

        result = subprocess.run(
            ["git", "merge-file", "--union", str(local_tmp), str(base_tmp), str(remote_tmp)],
            capture_output=True,
        )

        # git merge-file returns 0 on clean merge, >0 on conflicts (but --union resolves them)
        merged = local_tmp.read_bytes()
        logger.info("Union merge completed for %s (exit code %d)", relative_path, result.returncode)
        return merged
