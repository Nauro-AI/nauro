"""Cross-store parity for ``flag_question``.

The surface-level parity test (``test_flag_question_parity``) covers the
local adapters against the same ``FilesystemStore``. This file goes one
layer deeper: the same kernel call against an identically-seeded store
must produce the same :class:`FlagQuestionResult` regardless of which
``Store`` implementation is passed in.

The kernel reads ``open-questions.md`` and writes through
:meth:`Store.write_file`. Both operations sit on the locked Store
protocol, so a ``FilesystemStore`` and a ``CloudStore`` seeded with the
same initial content must produce byte-identical results.

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
from nauro_core.constants import OPEN_QUESTIONS_MD  # noqa: E402
from nauro_core.operations import flag_question  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-flag-question-cross-surface-test"
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


def _seed_open_questions(fs_store: FilesystemStore, cloud: CloudStore, body: str) -> None:
    fs_store.write_file(OPEN_QUESTIONS_MD, body)
    cloud.write_file(OPEN_QUESTIONS_MD, body)


def _dump(result) -> dict:
    return result.model_dump(mode="json", exclude_none=True)


def test_empty_store_matches_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_result = flag_question(fs_store, "Should we ship X?")
    cloud_result = flag_question(cloud, "Should we ship X?")

    assert _dump(fs_result) == _dump(cloud_result) == {"status": "ok", "num": 1}
    assert fs_store.read_file(OPEN_QUESTIONS_MD) == cloud.read_file(OPEN_QUESTIONS_MD)


def test_existing_entries_match_across_stores(both_stores):
    fs_store, cloud = both_stores
    seed = "# Open Questions\n- [Q3] Seeded one\n- [Q5] Seeded two\n"
    _seed_open_questions(fs_store, cloud, seed)

    fs_result = flag_question(fs_store, "Third question")
    cloud_result = flag_question(cloud, "Third question")

    assert _dump(fs_result) == _dump(cloud_result) == {"status": "ok", "num": 6}
    assert fs_store.read_file(OPEN_QUESTIONS_MD) == cloud.read_file(OPEN_QUESTIONS_MD)


def test_repeated_writes_match_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_one = flag_question(fs_store, "One")
    fs_two = flag_question(fs_store, "Two")
    cloud_one = flag_question(cloud, "One")
    cloud_two = flag_question(cloud, "Two")

    assert _dump(fs_one) == _dump(cloud_one) == {"status": "ok", "num": 1}
    assert _dump(fs_two) == _dump(cloud_two) == {"status": "ok", "num": 2}
    assert fs_store.read_file(OPEN_QUESTIONS_MD) == cloud.read_file(OPEN_QUESTIONS_MD)
