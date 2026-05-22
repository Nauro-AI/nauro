"""Shared warn-then-regen helper for AGENTS.md across write paths.

``nauro note`` and ``tool_propose_decision`` both refresh ``AGENTS.md`` in
every associated repo after writing a decision. The two paths previously
duplicated the registry-lookup / missing-repo-warning loop. This helper
collapses them into one shape so the warning message and skip behaviour
cannot drift between callers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nauro.store.registry import (
    RegistrySchemaError,
    get_project_v2,
    load_registry,
)
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
        warn: Optional callback for the per-repo "repo path does not exist"
            warning. When ``None`` (the MCP adapter case) missing repo
            paths are silently skipped.

    Returns:
        The list of repo paths whose ``AGENTS.md`` was successfully
        regenerated. Mirrors :func:`regenerate_agents_md_for_project` so
        existing CLI surfaces can continue echoing the per-repo line.
    """
    for repo_str in _registry_repo_paths(project_key):
        if not Path(repo_str).is_dir() and warn is not None:
            warn(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_str}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json"
            )
    return regenerate_agents_md_for_project(project_key, store_path)


def _registry_repo_paths(project_key: str) -> list[str]:
    """Return repo paths for ``project_key`` from v2 (preferred) or v1 registry."""
    try:
        v2_entry = get_project_v2(project_key)
    except RegistrySchemaError:
        v2_entry = None
    if v2_entry is not None:
        return list(v2_entry.get("repo_paths", []))
    registry = load_registry()
    return list(registry["projects"].get(project_key, {}).get("repo_paths", []))
