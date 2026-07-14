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
from nauro.templates.agents_md import (
    agents_md_is_safe_to_replace,
    regenerate_agents_md_for_project,
)


def warn_then_regen(
    project_key: str,
    store_path: Path,
    *,
    warn: Callable[[str], None] | None = None,
    preserve_unmanaged: bool = False,
    fail_soft: bool = False,
) -> list[Path]:
    """Warn on missing repo paths, then regenerate ``AGENTS.md`` everywhere.

    Args:
        project_key: Either a v2 project_id (ULID) or a v1 project name.
        store_path: Path to the project store directory.
        warn: Optional callback for missing-repo and git-hygiene warnings.
            When ``None``, missing repo paths are silently skipped and
            git-hygiene checks do not run.
        preserve_unmanaged: Leave an existing ``AGENTS.md`` untouched unless
            it carries Nauro's generation marker.
        fail_soft: Report filesystem errors through ``warn`` and return rather
            than raising after project registration has succeeded.

    Returns:
        The list of repo paths whose ``AGENTS.md`` was successfully
        regenerated. Mirrors :func:`regenerate_agents_md_for_project` so
        existing CLI surfaces can continue echoing the per-repo line.
    """
    repo_paths = [Path(repo_str) for repo_str in get_repo_paths(project_key)]
    for repo_path in repo_paths:
        if not repo_path.is_dir() and warn is not None:
            warn(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_path}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json"
            )

    try:
        if preserve_unmanaged:
            for repo_path in repo_paths:
                agents_md_path = repo_path / "AGENTS.md"
                if not agents_md_is_safe_to_replace(agents_md_path):
                    if warn is not None:
                        warn(
                            "  Warning: existing AGENTS.md is not Nauro-generated; "
                            f"left unchanged: {agents_md_path}"
                        )
        updated = regenerate_agents_md_for_project(
            project_key,
            store_path,
            preserve_unmanaged=preserve_unmanaged,
        )
    except OSError as exc:
        if not fail_soft:
            raise
        if warn is not None:
            warn(
                "  Warning: project registration succeeded, but AGENTS.md could not "
                f"be generated: {exc}\n"
                "  Fix the file permissions, then run 'nauro sync'."
            )
        return []
    if warn is not None:
        for repo_path in updated:
            for message in public_surface_git_warnings(repo_path, "AGENTS.md"):
                warn(message)
    return updated
