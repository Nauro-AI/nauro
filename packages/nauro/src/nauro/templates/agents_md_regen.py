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
from nauro.constants import AGENTS_MD
from nauro.store.registry import get_repo_paths
from nauro.store.write_safety import find_symlink
from nauro.templates.agents_md import (
    agents_md_is_safe_to_replace,
    regenerate_agents_md_for_project,
)


def warn_then_regen(
    project_key: str,
    store_path: Path,
    *,
    warn: Callable[[str], None] | None = None,
    overwrite_unmanaged: bool = False,
    fail_soft: bool = False,
) -> list[Path]:
    """Warn on skipped repo paths, then regenerate ``AGENTS.md`` everywhere.

    Args:
        project_key: Either a v2 project_id (ULID) or a v1 project name.
        store_path: Path to the project store directory.
        warn: Optional callback for skip and git-hygiene warnings. When
            ``None``, skipped repo paths are silent and git-hygiene checks
            do not run.
        overwrite_unmanaged: Replace an existing ``AGENTS.md`` even when
            Nauro did not generate it. Off by default: only ``nauro sync``
            passes True, every other caller preserves hand-written files.
        fail_soft: Report filesystem errors through ``warn`` and return rather
            than raising after project registration has succeeded.

    Returns:
        The list of repo paths whose ``AGENTS.md`` was successfully
        regenerated. Mirrors :func:`regenerate_agents_md_for_project` so
        existing CLI surfaces can continue echoing the per-repo line.
    """
    repo_paths = [Path(repo_str) for repo_str in get_repo_paths(project_key)]
    try:
        # Warn channel only: the regeneration itself re-applies every skip
        # below, so nothing outside a real checkout is ever written through.
        # One loop, first matching skip wins, so no repo path double-warns.
        # The ownership probe reads the existing file, so it stays inside the
        # try: a read-side permission error is covered by the same fail_soft
        # contract as a failed write.
        if warn is not None:
            for repo_path in repo_paths:
                if not repo_path.is_dir():
                    warn(
                        f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_path}\n"
                        f"  Fix: remove from registry or update path in ~/.nauro/registry.json"
                    )
                    continue
                refusal = find_symlink(repo_path, AGENTS_MD)
                if refusal is not None:
                    warn(f"  Warning: {refusal.message}")
                    continue
                agents_md_path = repo_path / AGENTS_MD
                if not overwrite_unmanaged and not agents_md_is_safe_to_replace(agents_md_path):
                    warn(
                        "  Warning: existing AGENTS.md is not Nauro-generated; "
                        f"left unchanged: {agents_md_path}"
                    )
        updated = regenerate_agents_md_for_project(
            project_key,
            store_path,
            overwrite_unmanaged=overwrite_unmanaged,
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
