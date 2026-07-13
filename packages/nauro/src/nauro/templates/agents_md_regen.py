"""Shared warn-then-regen helper for AGENTS.md across write paths.

``nauro note`` and ``tool_propose_decision`` both refresh ``AGENTS.md`` in
every associated repo after writing a decision. This helper owns the
registry-lookup / missing-repo-warning loop in one shape so the warning
message and skip behaviour cannot drift between callers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.store.registry import get_repo_paths
from nauro.templates.agents_md import regenerate_agents_md_for_project


def warn_then_regen(
    project_key: str,
    store_path: Path,
    *,
    warn: Callable[[str], None] | None = None,
) -> list[Path]:
    """Warn on missing repo paths, then regenerate ``AGENTS.md`` everywhere.

    Args:
        project_key: Either a v2 project_id (ULID) or a v1 project name.
        store_path: Path to the project store directory.
        warn: Optional callback for missing-repo and git-hygiene warnings.
            When ``None``, missing repo paths are silently skipped and
            git-hygiene checks do not run.

    Returns:
        The list of repo paths whose ``AGENTS.md`` was successfully
        regenerated. Mirrors :func:`regenerate_agents_md_for_project` so
        existing CLI surfaces can continue echoing the per-repo line.
    """
    for repo_str in get_repo_paths(project_key):
        if not Path(repo_str).is_dir() and warn is not None:
            warn(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_str}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json"
            )
    updated = regenerate_agents_md_for_project(project_key, store_path)
    if warn is not None:
        for repo_path in updated:
            for message in public_surface_git_warnings(repo_path, "AGENTS.md"):
                warn(message)
    return updated
