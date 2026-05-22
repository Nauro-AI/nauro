"""Cross-store parity for ``update_state``.

The surface-level parity test (``test_update_state_parity``) covers the
local adapters against the same ``FilesystemStore``. This file goes one
layer deeper: the same kernel call against an identically-seeded store
must produce the same :class:`UpdateStateResult` regardless of which
``Store`` implementation is passed in.

The kernel reads ``state_current.md`` (or migrates from legacy
``state.md``) and writes through :meth:`Store.write_file`. Both
operations sit on the locked Store protocol, so a ``FilesystemStore``
and a ``CloudStore`` seeded with the same initial state must produce
byte-identical results.

``CloudStore`` is consumed by other transports and is not always
installed alongside ``nauro``. When this test runs from inside the
nauro workspace alone, the module skips at load via
``pytest.importorskip``.
"""

from __future__ import annotations

import pytest

cloud_store_module = pytest.importorskip(
    "mcp_server.store.cloud_store",
    reason="CloudStore is consumed by other transports; not always installed alongside nauro.",
)
boto3 = pytest.importorskip("boto3", reason="boto3 needed to seed a moto S3 bucket.")
moto = pytest.importorskip("moto", reason="moto needed for in-memory S3.")

# Imports below ``importorskip`` deliberately run only when both packages are
# installed; ruff E402 does not apply here.
from nauro_core.constants import (  # noqa: E402
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_LEGACY_FILENAME,
)
from nauro_core.operations import update_state  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-update-state-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair backed by separate roots."""
    monkeypatch.setenv("NAURO_S3_BUCKET", TEST_BUCKET)

    with moto.mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=TEST_BUCKET)

        fs_store = FilesystemStore(tmp_path)
        cloud = CloudStore(user_id=TEST_USER_ID, project_id=TEST_PROJECT_ID)
        yield fs_store, cloud


def _seed_current(fs_store: FilesystemStore, cloud: CloudStore, body: str) -> None:
    fs_store.write_file(STATE_CURRENT_FILENAME, body)
    cloud.write_file(STATE_CURRENT_FILENAME, body)


def _seed_legacy(fs_store: FilesystemStore, cloud: CloudStore, body: str) -> None:
    fs_store.write_file(STATE_LEGACY_FILENAME, body)
    cloud.write_file(STATE_LEGACY_FILENAME, body)


def _dump(result) -> dict:
    return result.model_dump(mode="json", exclude_none=True)


def test_noop_branch_matches_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_result = update_state(fs_store, "anything")
    cloud_result = update_state(cloud, "anything")

    assert _dump(fs_result) == _dump(cloud_result) == {"status": "noop"}


def test_ok_branch_matches_across_stores(both_stores):
    fs_store, cloud = both_stores
    _seed_current(fs_store, cloud, "# Current State\n\n- Task one\n")

    fs_result = update_state(fs_store, "Task two")
    cloud_result = update_state(cloud, "Task two")

    assert _dump(fs_result) == _dump(cloud_result) == {"status": "ok"}
    # Both stores must hold the same post-write content.
    assert fs_store.read_file(STATE_CURRENT_FILENAME) == cloud.read_file(STATE_CURRENT_FILENAME)
    assert fs_store.read_file(STATE_HISTORY_FILENAME) == cloud.read_file(STATE_HISTORY_FILENAME)


def test_warning_branch_matches_across_stores(both_stores):
    fs_store, cloud = both_stores
    _seed_current(fs_store, cloud, "- Implemented OAuth login flow with PKCE\n")

    fs_result = update_state(fs_store, "Implemented OAuth refresh logic with PKCE")
    cloud_result = update_state(cloud, "Implemented OAuth refresh logic with PKCE")

    assert _dump(fs_result) == _dump(cloud_result)
    assert fs_result.warning is not None
    assert "keywords" in fs_result.warning.lower()


def test_legacy_migration_matches_across_stores(both_stores):
    fs_store, cloud = both_stores
    _seed_legacy(fs_store, cloud, "# State\n\n## Current\nLegacy content\n")

    fs_result = update_state(fs_store, "Post-upgrade task")
    cloud_result = update_state(cloud, "Post-upgrade task")

    assert _dump(fs_result) == _dump(cloud_result) == {"status": "ok"}
    # The migrated current and the archived legacy body must match between stores.
    assert fs_store.read_file(STATE_CURRENT_FILENAME) == cloud.read_file(STATE_CURRENT_FILENAME)
    assert fs_store.read_file(STATE_HISTORY_FILENAME) == cloud.read_file(STATE_HISTORY_FILENAME)
