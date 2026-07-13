"""Shared helpers for the cross-surface parity tests.

Each ``test_*_cross_surface.py`` module seeds a ``FilesystemStore`` and a
``mcp_server.store.cloud_store.CloudStore`` with identical content, then asserts
the operations kernel returns byte-identical envelopes across both surfaces. The
seeding helpers and the envelope-dump shim were duplicated per module; they live
here now, parameterized by each module's ``SEED_DECISIONS`` / ``SEED_FILES`` maps.

``CloudStore`` ships in the private mcp-server repo and is absent from the public
monorepo CI, where every cross-surface module skips at load via
``pytest.importorskip``. This conftest is imported unconditionally by pytest, so
it must stay importable without ``mcp_server`` / ``boto3`` / ``moto``: the
CloudStore import is deferred into ``seed_cloud_store`` and runs only once a test
that cleared its own importorskip gate calls it.
"""

from __future__ import annotations

from pathlib import Path

from nauro.store.filesystem_store import FilesystemStore
from tests.conftest import (
    CROSS_SURFACE_BUCKET,
    CROSS_SURFACE_PROJECT_ID,
    CROSS_SURFACE_USER_ID,
    cloud_prefix,
)


def seed_filesystem_store(
    root: Path,
    decisions: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
) -> FilesystemStore:
    """Seed a ``FilesystemStore`` rooted at ``root`` with the given content.

    Plain ``files`` land at the store root; ``decisions`` land under
    ``decisions/<stem>.md``. Either mapping may be omitted.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, body in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    if decisions:
        decisions_dir = root / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        for stem, body in decisions.items():
            (decisions_dir / f"{stem}.md").write_text(body)
    return FilesystemStore(root)


def seed_cloud_store(
    s3_client,
    decisions: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
):
    """Seed a moto-backed ``CloudStore`` with the given content.

    ``CloudStore`` is imported lazily so this module stays importable when the
    private mcp-server package is absent.
    """
    from mcp_server.store.cloud_store import CloudStore

    prefix = cloud_prefix(CROSS_SURFACE_USER_ID, CROSS_SURFACE_PROJECT_ID)
    for name, body in (files or {}).items():
        s3_client.put_object(
            Bucket=CROSS_SURFACE_BUCKET,
            Key=f"{prefix}/{name}",
            Body=body.encode(),
        )
    for stem, body in (decisions or {}).items():
        s3_client.put_object(
            Bucket=CROSS_SURFACE_BUCKET,
            Key=f"{prefix}/decisions/{stem}.md",
            Body=body.encode(),
        )
    return CloudStore(user_id=CROSS_SURFACE_USER_ID, project_id=CROSS_SURFACE_PROJECT_ID)


def _dump(result) -> dict:
    """Return the JSON-mode model dump with ``None`` fields dropped."""
    return result.model_dump(mode="json", exclude_none=True)
