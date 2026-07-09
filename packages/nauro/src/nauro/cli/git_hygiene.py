"""Advisory git hygiene warnings for Nauro-generated public surfaces."""

from __future__ import annotations

import subprocess
from pathlib import Path

KNOWN_PUBLIC_SURFACE_PATHS = frozenset(
    {
        "AGENTS.md",
        ".mcp.json",
        ".cursor/mcp.json",
        ".claude/settings.json",
        ".nauro/config.json",
    }
)


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
