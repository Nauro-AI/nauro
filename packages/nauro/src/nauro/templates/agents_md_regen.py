"""Shared warn-then-regen seam for AGENTS.md.

Every write, setup, sync, init, attach and reconnect path refreshes
``AGENTS.md`` in each associated repo through this helper. It owns the
registry lookup and the skip warnings in one shape, so the warning text and
skip behaviour cannot drift between callers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nauro.setup.git_hygiene import public_surface_git_warnings
from nauro.setup.outcomes import BridgeKind, BridgeOutcome
from nauro.setup.render import render
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.registry import get_repo_paths
from nauro.templates.agents_md import (
    agents_md_skip_warning,
    regenerate_agents_md_for_project,
)


def warn_then_regen(
    project_key: str,
    store_path: Path,
    *,
    warn: Callable[[str], None] | None = None,
    overwrite_unmanaged: bool = False,
    fail_soft: bool = False,
    bridge_sink: list[BridgeOutcome] | None = None,
    surface_bridge_notices: bool = True,
    snapshot: GenerationSnapshotStore | None = None,
) -> list[Path]:
    """Regenerate ``AGENTS.md`` in each project repo that passes the write guards, returning
    updated paths. ``fail_soft`` maps regeneration OSError to [] (warned if ``warn`` is set).
    Sink-less actionable bridge notices reach it unless ``surface_bridge_notices`` is False."""
    # Always collect bridge outcomes so a sink-less caller can still surface the
    # actionable ones; a supplied sink is filled in place for the caller.
    collected: list[BridgeOutcome] = bridge_sink if bridge_sink is not None else []
    repo_paths = [Path(repo_str) for repo_str in get_repo_paths(project_key)]
    try:
        # Warn channel only: the regeneration applies the same skip guard, so
        # nothing outside a real checkout is ever written through.
        # The ownership probe reads the existing file, so it stays inside the
        # try: a read-side permission error is covered by the same fail_soft
        # contract as a failed write.
        if warn is not None:
            for repo_path in repo_paths:
                message = agents_md_skip_warning(repo_path, overwrite_unmanaged=overwrite_unmanaged)
                if message is not None:
                    warn(message)
        updated = regenerate_agents_md_for_project(
            project_key,
            store_path,
            overwrite_unmanaged=overwrite_unmanaged,
            bridge_sink=collected,
            snapshot=snapshot,
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
        actionable = (BridgeKind.ADVISORY, BridgeKind.FAILED, BridgeKind.REFUSED_SYMLINK)
        for outcome in collected:
            if outcome.kind in actionable:
                for line in render(outcome):
                    warn(line)
    return updated
