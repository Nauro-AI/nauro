"""Shared helpers for the test_sync test suite."""

from __future__ import annotations

from pathlib import Path

from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

CLOUD_PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _scaffolded_cloud_project(name: str, repo_path: Path, project_id: str | None = None) -> Path:
    """Register a cloud-mode v2 project, scaffold its store, and return the store path.

    When ``project_id`` is omitted a ULID is minted automatically. Tests that
    pass a project id to a hook (``pull_before_session`` / ``push_after_write``)
    or presign call must supply ``project_id`` explicitly here so the registry
    lookup that gates those paths (``is_cloud_project``) resolves the same id —
    otherwise the hook early-returns at the project-not-found gate and the test
    silently exercises a shallower path.
    """
    _pid, store = register_project_v2(
        name,
        [repo_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        server_url="https://example.test",
        project_id=project_id,
    )
    scaffold_project_store(name, store)
    return store
