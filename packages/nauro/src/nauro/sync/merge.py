"""Conflict resolution for cloud sync.

When both local and remote changed since last sync, this module decides
how to merge or which version wins.
"""

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
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
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = relative_path.replace("/", "_")
    backup_dir = project_path / ".conflict-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{timestamp}-{filename}"
    backup_path.write_bytes(content)
    logger.info("Conflict backup saved: %s", backup_path)
    return backup_path


_SET_UNION_PATHS = ("open-questions.md", "state_history.md")


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

    if relative_path in _SET_UNION_PATHS:
        return _set_union_markdown(local_content, remote_content)

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


def _parse_sections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split lines into a preamble and a list of (header, body) sections.

    A section starts at any line beginning with "## " (level-2 ATX heading).
    The preamble is everything before the first such header. Each section body
    runs until the next "## " line or end of input.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if current_header is None:
                # Close out the preamble; start the first section.
                pass
            else:
                sections.append((current_header, current_body))
            current_header = line
            current_body = []
            continue
        if current_header is None:
            preamble.append(line)
        else:
            current_body.append(line)

    if current_header is not None:
        sections.append((current_header, current_body))

    return preamble, sections


def _dedupe_preserve_order(lines: list[str]) -> list[str]:
    """Drop exact-duplicate non-blank lines, preserving first occurrence order.

    Blank lines are passed through unchanged (not deduped), so the merged
    output keeps the visual structure of the inputs.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line == "":
            out.append(line)
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _set_union_markdown(local: bytes, remote: bytes) -> bytes:
    """Section-aware set-union merge for append-only markdown files.

    Parses each side into a preamble plus a list of "## " sections, then emits
    the deduped union of the preambles followed by, for each section header in
    local order, the deduped union of that section's local and remote bodies.
    Any sections that appear only in remote are appended at the end.

    Pure function (no I/O); plain string ops only.
    """
    local_text = local.decode("utf-8")
    remote_text = remote.decode("utf-8")

    local_lines = local_text.split("\n")
    remote_lines = remote_text.split("\n")

    # split("\n") on a trailing-newline string yields a final "" element. That's
    # actual content for the dedupe step (blank lines are preserved), so we drop
    # the synthetic trailing "" and re-add a single newline at the end.
    local_trailing_nl = local_text.endswith("\n")
    remote_trailing_nl = remote_text.endswith("\n")
    if local_trailing_nl and local_lines and local_lines[-1] == "":
        local_lines = local_lines[:-1]
    if remote_trailing_nl and remote_lines and remote_lines[-1] == "":
        remote_lines = remote_lines[:-1]

    local_preamble, local_sections = _parse_sections(local_lines)
    remote_preamble, remote_sections = _parse_sections(remote_lines)

    # Group sections by header so a header that appears multiple times in one
    # source (the corrupted-file case where the whole document was duplicated)
    # collapses into a single emitted section with the union of all bodies.
    section_order: list[str] = []
    bodies_by_header: dict[str, list[str]] = {}
    for header, body in list(local_sections) + list(remote_sections):
        if header not in bodies_by_header:
            section_order.append(header)
            bodies_by_header[header] = []
        bodies_by_header[header].extend(body)

    merged: list[str] = []
    merged.extend(_dedupe_preserve_order(local_preamble + remote_preamble))

    for header in section_order:
        merged.append(header)
        merged.extend(_dedupe_preserve_order(bodies_by_header[header]))

    # Final pass: dedupe non-blank lines across the whole document. Within each
    # section the body has already been deduped against itself, but a corrupted
    # file may carry a stray "# Title" line (or repeated entries) inside a
    # section body that also lives in the preamble. Drop those exact-duplicate
    # non-blank lines while preserving blanks and first-occurrence order.
    deduped = _dedupe_preserve_order(merged)

    result = "\n".join(deduped)
    if local_trailing_nl or remote_trailing_nl:
        result += "\n"
    return result.encode("utf-8")
