"""Shared warn-then-regen helper for AGENTS.md across write paths.

``nauro note`` and ``tool_propose_decision`` both refresh ``AGENTS.md`` in
every associated repo after writing a decision. This helper owns the
registry-lookup / missing-repo-warning loop in one shape so the warning
message and skip behaviour cannot drift between callers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.constants import AGENTS_MD
from nauro.store.registry import get_repo_paths
from nauro.store.write_safety import find_symlink
from nauro.templates.agents_md import (
    agents_md_is_safe_to_replace,
    regenerate_agents_md_for_project,
)

if TYPE_CHECKING:
    from nauro.cli.integrations.outcomes import BridgeOutcome


def warn_then_regen(
    project_key: str,
    store_path: Path,
    *,
    warn: Callable[[str], None] | None = None,
    overwrite_unmanaged: bool = False,
    fail_soft: bool = False,
    bridge_sink: list[BridgeOutcome] | None = None,
    surface_bridge_notices: bool = True,
) -> list[Path]:
    """Regenerate ``AGENTS.md`` in each project repo that passes the write guards, returning
    updated paths. ``fail_soft`` maps regeneration OSError to [] (warned if ``warn`` is set).
    Sink-less actionable bridge notices reach it unless ``surface_bridge_notices`` is False."""
    # Always collect bridge outcomes so a sink-less caller can still surface the
    # actionable ones; a supplied sink is filled in place for the caller.
    collected: list[BridgeOutcome] = bridge_sink if bridge_sink is not None else []
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
            bridge_sink=collected,
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
    # A sink-less caller renders nothing itself, so surface the actionable
    # bridge outcomes through the same warn channel; a sink caller already
    # renders them and is not double-reported.
    if bridge_sink is None and warn is not None and surface_bridge_notices:
        from nauro.cli.integrations.outcomes import BridgeKind
        from nauro.cli.integrations.render import render

        actionable = (BridgeKind.ADVISORY, BridgeKind.FAILED, BridgeKind.REFUSED_SYMLINK)
        for outcome in collected:
            if outcome.kind in actionable:
                for line in render(outcome):
                    warn(line)
    return updated
