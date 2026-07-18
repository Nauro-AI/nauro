"""Git hygiene for Nauro-generated surfaces: advisory warnings and the
machine-local wiring policy.

Two layers live here. The advisory layer (``public_surface_git_warnings``)
warns when a known Nauro-written path is tracked or easy to add by accident.
The enforcement layer applies only to machine-local wiring files — configs
that record absolute binary paths and therefore can never work on another
machine: ``wiring_path_is_tracked`` lets codecs refuse to write wiring into a
git-tracked file, and ``ensure_wiring_ignored`` / ``remove_wiring_ignore_entry``
maintain a marker-delimited managed block in the repo's ``.gitignore`` so each
machine regenerates wiring that is ignored on arrival. Identity surfaces
(``.nauro/config.json``, ``AGENTS.md``) are meant to be committed and stay in
the advisory layer only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import SymlinkRefusal, find_symlink

KNOWN_PUBLIC_SURFACE_PATHS = frozenset(
    {
        "AGENTS.md",
        ".mcp.json",
        ".cursor/mcp.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".codex/hooks.json",
        ".nauro/config.json",
    }
)

# Marker lines delimiting the Nauro-managed entries in a repo's .gitignore.
# Everything between them is Nauro-owned; user lines outside are never touched.
GITIGNORE_BLOCK_BEGIN = "# >>> nauro: machine-local wiring (regenerated per machine) >>>"
GITIGNORE_BLOCK_END = "# <<< nauro <<<"


class GitIgnoreKind(Enum):
    ADDED = auto()
    ALREADY_COVERED = auto()
    SKIPPED_NON_GIT = auto()
    REFUSED_SYMLINK = auto()
    REFUSED_UNREADABLE = auto()
    REFUSED_MALFORMED_BLOCK = auto()
    REFUSED_UNWRITABLE = auto()
    REMOVED_ENTRY = auto()
    REMOVED_BLOCK = auto()
    NOTHING_TO_REMOVE = auto()


@dataclass(frozen=True)
class GitIgnoreResult:
    """Result of maintaining a wiring entry in the managed .gitignore block."""

    kind: GitIgnoreKind
    rel_path: str
    refusal: SymlinkRefusal | None = None
    detail: str | None = None


def wiring_path_is_tracked(repo_root: Path, rel_path: str) -> bool:
    """True iff ``rel_path`` under ``repo_root`` is tracked by git.

    Wiring codecs use this to refuse writing a machine-local absolute path
    into a file whose next commit would ship that path to every clone.
    Soft-fails to False (non-git dir, git missing) so bare setup keeps working.
    """
    git_root = _git_root(repo_root)
    if git_root is None:
        return False
    git_rel = _git_rel(git_root, repo_root, rel_path)
    if git_rel is None:
        return False
    return _is_tracked(git_root, git_rel)


def ensure_wiring_ignored(repo_root: Path, rel_path: str) -> GitIgnoreResult:
    """Ensure ``rel_path`` is git-ignored via the managed block in ``.gitignore``.

    No-ops when the path is already effectively ignored (a user rule or an
    existing block entry), so user ignore rules are never duplicated. Entries
    are written with a leading slash so they anchor to the repo root instead of
    matching in every subdirectory.
    """
    git_root = _git_root(repo_root)
    if git_root is None:
        return GitIgnoreResult(GitIgnoreKind.SKIPPED_NON_GIT, rel_path)
    git_rel = _git_rel(git_root, repo_root, rel_path)
    if git_rel is None:
        return GitIgnoreResult(GitIgnoreKind.SKIPPED_NON_GIT, rel_path)
    if _is_ignored(git_root, git_rel):
        return GitIgnoreResult(GitIgnoreKind.ALREADY_COVERED, rel_path)

    refusal = find_symlink(repo_root, ".gitignore")
    if refusal is not None:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_SYMLINK, rel_path, refusal=refusal)

    gitignore_path = repo_root / ".gitignore"
    lines = _read_gitignore_lines(gitignore_path)
    if lines is None:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_UNREADABLE, rel_path)

    entry = f"/{rel_path}"
    block = _find_block(lines)
    if block is None:
        # An orphaned begin marker (its end line hand-deleted) must refuse:
        # appending a second block would let a later removal treat everything
        # from the orphan down to the new end marker as Nauro-owned.
        if GITIGNORE_BLOCK_BEGIN in lines:
            return GitIgnoreResult(GitIgnoreKind.REFUSED_MALFORMED_BLOCK, rel_path)
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([GITIGNORE_BLOCK_BEGIN, entry, GITIGNORE_BLOCK_END])
    else:
        begin, end = block
        if entry in lines[begin + 1 : end]:
            return GitIgnoreResult(GitIgnoreKind.ALREADY_COVERED, rel_path)
        lines.insert(end, entry)

    # A failed .gitignore write (unwritable repo root, disk error) must stay a
    # typed refusal on the codec outcome, never an exception that swallows the
    # surface's own status line.
    try:
        atomic_write_text(gitignore_path, "\n".join(lines) + "\n")
    except OSError as exc:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_UNWRITABLE, rel_path, detail=str(exc))
    return GitIgnoreResult(GitIgnoreKind.ADDED, rel_path)


def remove_wiring_ignore_entry(repo_root: Path, rel_path: str) -> GitIgnoreResult:
    """Drop ``rel_path`` from the managed block; drop the block when it empties.

    User lines outside the markers are never touched. When removing the block
    leaves ``.gitignore`` empty, the file is deleted (mirroring codecs that
    unlink a config emptied of its last entry).
    """
    git_root = _git_root(repo_root)
    if git_root is None:
        return GitIgnoreResult(GitIgnoreKind.SKIPPED_NON_GIT, rel_path)

    refusal = find_symlink(repo_root, ".gitignore")
    if refusal is not None:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_SYMLINK, rel_path, refusal=refusal)

    gitignore_path = repo_root / ".gitignore"
    if not gitignore_path.exists():
        return GitIgnoreResult(GitIgnoreKind.NOTHING_TO_REMOVE, rel_path)
    lines = _read_gitignore_lines(gitignore_path)
    if lines is None:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_UNREADABLE, rel_path)

    block = _find_block(lines)
    entry = f"/{rel_path}"
    if block is None:
        # Mirror of the add path's orphan guard: with no valid marker pair,
        # nothing here is provably Nauro-owned, so nothing is deleted.
        if GITIGNORE_BLOCK_BEGIN in lines:
            return GitIgnoreResult(GitIgnoreKind.REFUSED_MALFORMED_BLOCK, rel_path)
        return GitIgnoreResult(GitIgnoreKind.NOTHING_TO_REMOVE, rel_path)
    if entry not in lines[block[0] + 1 : block[1]]:
        return GitIgnoreResult(GitIgnoreKind.NOTHING_TO_REMOVE, rel_path)

    begin, end = block
    remaining_entries = [line for line in lines[begin + 1 : end] if line != entry]
    if remaining_entries:
        lines[begin + 1 : end] = remaining_entries
        kind = GitIgnoreKind.REMOVED_ENTRY
    else:
        # Last entry: drop the whole block, plus the single separator blank
        # line the add path inserted before it.
        del lines[begin : end + 1]
        if begin > 0 and not lines[begin - 1].strip():
            del lines[begin - 1]
        kind = GitIgnoreKind.REMOVED_BLOCK

    try:
        if any(line.strip() for line in lines):
            atomic_write_text(gitignore_path, "\n".join(lines) + "\n")
        else:
            gitignore_path.unlink()
    except OSError as exc:
        return GitIgnoreResult(GitIgnoreKind.REFUSED_UNWRITABLE, rel_path, detail=str(exc))
    return GitIgnoreResult(kind, rel_path)


def _git_rel(git_root: Path, repo_root: Path, rel_path: str) -> str | None:
    try:
        return (repo_root / rel_path).resolve().relative_to(git_root).as_posix()
    except ValueError:
        return None


def _read_gitignore_lines(gitignore_path: Path) -> list[str] | None:
    """Read ``.gitignore`` as lines; [] when absent, None when not UTF-8."""
    if not gitignore_path.exists():
        return []
    try:
        return gitignore_path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def _find_block(lines: list[str]) -> tuple[int, int] | None:
    """Return (begin, end) indices of the managed block markers, or None."""
    try:
        begin = lines.index(GITIGNORE_BLOCK_BEGIN)
        end = lines.index(GITIGNORE_BLOCK_END, begin + 1)
    except ValueError:
        return None
    return begin, end


def public_surface_git_warnings(repo_root: Path, rel_path: str) -> list[str]:
    """Return advisory warnings for a known path Nauro just wrote."""
    if rel_path not in KNOWN_PUBLIC_SURFACE_PATHS:
        return []

    target = repo_root / rel_path
    if not target.exists():
        return []

    git_root = _git_root(repo_root)
    if git_root is None:
        return []

    try:
        git_rel = target.resolve().relative_to(git_root).as_posix()
    except ValueError:
        return []

    if _is_tracked(git_root, git_rel):
        return [_tracked_warning(rel_path)]
    if _is_ignored(git_root, git_rel):
        return []
    return [_untracked_warning(rel_path)]


def _git_root(path: Path) -> Path | None:
    proc = _run_git(path, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root).resolve() if root else None


def _is_tracked(git_root: Path, git_rel: str) -> bool:
    proc = _run_git(git_root, "ls-files", "--error-unmatch", "--", git_rel)
    return proc is not None and proc.returncode == 0


def _is_ignored(git_root: Path, git_rel: str) -> bool:
    proc = _run_git(git_root, "check-ignore", "-q", "--", git_rel)
    return proc is not None and proc.returncode == 0


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None


def _tracked_warning(rel_path: str) -> str:
    if rel_path == ".nauro/config.json":
        detail = "it is repo-local Nauro project config"
    elif rel_path == "AGENTS.md":
        detail = "it may expose generated Nauro context"
    else:
        detail = "it may expose local Nauro wiring"
    return (
        f"  Warning: {rel_path} is tracked by git. "
        f"Review before publishing a public-bound repo; {detail}."
    )


def _untracked_warning(rel_path: str) -> str:
    if rel_path == ".nauro/config.json":
        detail = "It is repo-local Nauro project config"
    elif rel_path == "AGENTS.md":
        detail = "It contains generated Nauro context"
    else:
        detail = "It contains local Nauro wiring"
    return (
        f"  Note: {rel_path} is untracked and not git-ignored. "
        "It is easy to add by accident; review or ignore it before publishing "
        f"a public-bound repo. {detail}."
    )
