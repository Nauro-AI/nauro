"""Layer-3 cross-surface parity for ``get_raw_file``.

The surface-level parity test (``test_get_raw_file_parity``) covers the
local adapters against the same ``FilesystemStore``. This file goes one
layer deeper: the same kernel call against ``FilesystemStore`` and
``mcp_server.store.cloud_store.CloudStore`` must produce the same
:class:`GetRawFileResult` for the same fixed inputs. That is the
no-drift-by-construction guarantee the operations-kernel doctrine
commits to.

CloudStore is consumed by other transports and is not always
installed in the same environment as ``nauro``. When this test runs
from inside the nauro workspace alone, ``mcp_server`` is unavailable
and the test skips at module load via ``pytest.importorskip``.
Engineers who have both packages on a single ``PYTHONPATH`` exercise
it locally to catch local-vs-cloud envelope drift before merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cloud_store_module = pytest.importorskip(
    "mcp_server.store.cloud_store",
    reason="CloudStore is provided by the private mcp-server repo.",
)
pytest.importorskip("boto3", reason="boto3 needed to seed a moto S3 bucket.")
pytest.importorskip("moto", reason="moto needed for in-memory S3.")

# Imports below ``importorskip`` deliberately run only when both packages are
# installed; ruff E402 does not apply here.
from nauro_core.operations import get_raw_file  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402
from tests.conftest import (  # noqa: E402
    CROSS_SURFACE_BUCKET,
    CROSS_SURFACE_PROJECT_ID,
    CROSS_SURFACE_USER_ID,
    cloud_prefix,
    moto_s3_bucket,
)

CloudStore = cloud_store_module.CloudStore

EXISTING_PATH = "project.md"
MISSING_PATH = "does-not-exist.md"

# Fixed seed: a single plain file mirrored into both stores so the
# input envelope is identical by construction; any envelope drift
# between the two surfaces is then a real bug, not a fixture mismatch.
SEED_FILES: dict[str, str] = {
    EXISTING_PATH: "# Parity Project\n\nA tiny file used by cross-surface parity.\n",
}


def _seed_filesystem_store(root: Path) -> FilesystemStore:
    root.mkdir(parents=True, exist_ok=True)
    for path, body in SEED_FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return FilesystemStore(root)


def _seed_cloud_store(s3_client) -> CloudStore:
    prefix = cloud_prefix(CROSS_SURFACE_USER_ID, CROSS_SURFACE_PROJECT_ID)
    for path, body in SEED_FILES.items():
        s3_client.put_object(
            Bucket=CROSS_SURFACE_BUCKET,
            Key=f"{prefix}/{path}",
            Body=body.encode(),
        )
    return CloudStore(user_id=CROSS_SURFACE_USER_ID, project_id=CROSS_SURFACE_PROJECT_ID)


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair seeded with identical files.

    The cloud half lives inside a moto-mocked S3 + an env-pointed bucket so
    the test never touches AWS.
    """
    with moto_s3_bucket(monkeypatch) as s3_client:
        fs_store = _seed_filesystem_store(tmp_path)
        cloud = _seed_cloud_store(s3_client)
        yield fs_store, cloud


def test_get_raw_file_result_matches_across_stores_on_hit(both_stores):
    fs_store, cloud = both_stores

    fs_result = get_raw_file(fs_store, EXISTING_PATH)
    cloud_result = get_raw_file(cloud, EXISTING_PATH)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
    assert fs_result.error is None
    assert fs_result.content is not None


def test_get_raw_file_result_matches_across_stores_on_miss(both_stores):
    fs_store, cloud = both_stores

    fs_result = get_raw_file(fs_store, MISSING_PATH)
    cloud_result = get_raw_file(cloud, MISSING_PATH)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
    assert fs_result.content is None
    assert fs_result.error is not None
    assert fs_result.error.kind == "error"
    assert fs_result.error.reason == f"File not found: {MISSING_PATH}"
