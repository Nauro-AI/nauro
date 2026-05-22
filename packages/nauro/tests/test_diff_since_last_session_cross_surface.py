"""Layer-3 cross-surface parity for ``diff_since_last_session``.

The surface-level parity test (``test_diff_since_last_session_parity``)
covers the local adapters against the same ``FilesystemStore``. This
file goes one layer deeper: the same kernel call against in-memory
snapshot dicts must produce the same :class:`DiffSinceLastSessionResult`
regardless of which ``Store`` implementation is passed as the
kernel-shape argument. That is the no-drift-by-construction guarantee
the operations-kernel doctrine commits to.

The kernel for this operation does not read from the ``Store`` — the
diff body operates entirely on the supplied snapshot dicts — so this
test pins that fact: passing ``FilesystemStore`` vs ``CloudStore`` must
yield byte-identical results for identical inputs.

CloudStore is consumed by other transports and is not always installed
in the same environment as ``nauro``. When this test runs from inside
the nauro workspace alone, ``mcp_server`` is unavailable and the test
skips at module load via ``pytest.importorskip``. Engineers who have
both packages on a single ``PYTHONPATH`` exercise it locally to catch
local-vs-cloud envelope drift before merge.
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
from nauro_core.operations import diff_since_last_session  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-diff-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"


BASELINE_SNAPSHOT: dict = {
    "version": 1,
    "timestamp": "2026-05-01T10:00:00+00:00",
    "files": {
        "state_current.md": "# Current State\n\n**Sprint:** alpha\n\n- Set up CI\n",
        "stack.md": "# Stack\n- Python 3.11\n",
        "open-questions.md": "# Open Questions\n- [Q1] Pick a queue?\n",
    },
}

LATEST_SNAPSHOT: dict = {
    "version": 2,
    "timestamp": "2026-05-02T10:00:00+00:00",
    "files": {
        "state_current.md": "# Current State\n\n**Sprint:** beta\n\n- Set up CI\n",
        "stack.md": "# Stack\n- Python 3.11\n- PostgreSQL\n",
        "open-questions.md": "# Open Questions\n- [Q1] Pick a queue?\n",
        "decisions/001-adopt-postgres.md": "# Adopt Postgres\n\nReasoned.\n",
    },
}


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair — kernel ignores both."""
    monkeypatch.setenv("NAURO_S3_BUCKET", TEST_BUCKET)

    with moto.mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=TEST_BUCKET)

        fs_store = FilesystemStore(tmp_path)
        cloud = CloudStore(user_id=TEST_USER_ID, project_id=TEST_PROJECT_ID)
        yield fs_store, cloud


def test_diff_result_matches_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_result = diff_since_last_session(fs_store, BASELINE_SNAPSHOT, LATEST_SNAPSHOT)
    cloud_result = diff_since_last_session(cloud, BASELINE_SNAPSHOT, LATEST_SNAPSHOT)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )


def test_sentinel_branches_match_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_empty = diff_since_last_session(fs_store, None, None)
    cloud_empty = diff_since_last_session(cloud, None, None)
    assert fs_empty == cloud_empty

    fs_one = diff_since_last_session(fs_store, None, LATEST_SNAPSHOT)
    cloud_one = diff_since_last_session(cloud, None, LATEST_SNAPSHOT)
    assert fs_one == cloud_one


def test_cutoff_date_used_threads_identically(both_stores):
    fs_store, cloud = both_stores
    cutoff = "2026-04-24T10:00:00+00:00"

    fs_result = diff_since_last_session(
        fs_store, BASELINE_SNAPSHOT, LATEST_SNAPSHOT, cutoff_date_used=cutoff
    )
    cloud_result = diff_since_last_session(
        cloud, BASELINE_SNAPSHOT, LATEST_SNAPSHOT, cutoff_date_used=cutoff
    )
    assert fs_result == cloud_result
    assert fs_result.cutoff_date_used == cutoff
