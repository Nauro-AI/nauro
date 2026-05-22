"""Cross-store parity for ``confirm_decision``.

The surface-level parity test (``test_confirm_decision_parity``) covers
the local adapters against the same ``FilesystemStore``. This file goes
one layer deeper: the same kernel call against an identically-seeded
store must produce the same :class:`ConfirmDecisionResult` regardless of
which ``Store`` implementation is passed in.

The confirm kernel writes through :meth:`Store.write_file` and reads
through :meth:`Store.list_decisions` / :meth:`Store.read_decision` /
:meth:`Store.read_file`. All four operations sit on the locked Store
protocol, so a ``FilesystemStore`` and a ``CloudStore`` seeded with the
same pending entry must produce byte-identical decision files (for the
add and supersede paths), byte-identical updated decision bodies (for
the update path), and byte-identical ``open-questions.md`` (for the
``resolves_questions`` path). The unknown-id branch must also produce
envelope-identical rejections.

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
from nauro_core.operations import confirm_decision  # noqa: E402
from nauro_core.operations.propose_decision import (  # noqa: E402
    _get_pending_store,
    _write_decision_direct,
)

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-confirm-decision-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


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


def _seed_postgres(store) -> str:
    return _write_decision_direct(
        store,
        {
            "title": "Adopt PostgreSQL primary database",
            "rationale": (
                "Mature ecosystem with strong JSON support and excellent tooling for our workload."
            ),
            "confidence": "high",
        },
    )


def _dump(result) -> dict:
    return result.model_dump(mode="json", exclude_none=True)


def _seed_pending_add(*, title: str, rationale: str, resolves: list[str] | None = None) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": title,
                "rationale": rationale,
                "confidence": "high",
                "resolves_questions": resolves or [],
            },
            "operation": "add",
            "affected_decision_id": None,
        },
        {"tier": 1, "operation": "add", "similar_decisions": [], "assessment": "seed"},
    )


def _seed_pending_supersede(affected_decision_id: str) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": "Switch to managed PostgreSQL provider",
                "rationale": (
                    "Reduces operational burden; the self-hosting rationale no longer applies."
                ),
                "confidence": "high",
            },
            "operation": "supersede",
            "affected_decision_id": affected_decision_id,
        },
        {"tier": 2, "operation": "supersede", "similar_decisions": [], "assessment": "seed"},
    )


def _seed_pending_update(affected_decision_id: str) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": "",
                "rationale": "Adds a managed-extensions clause to the existing PostgreSQL choice.",
            },
            "operation": "update",
            "affected_decision_id": affected_decision_id,
        },
        {"tier": 2, "operation": "update", "similar_decisions": [], "assessment": "seed"},
    )


def test_confirm_add_byte_identical(both_stores):
    fs_store, cloud = both_stores

    fs_confirm = _seed_pending_add(
        title="Adopt Redis for hot caching layer",
        rationale="In-memory cache for the hot read paths across the API tier with pub/sub.",
    )
    fs_result = confirm_decision(fs_store, fs_confirm)

    cloud_confirm = _seed_pending_add(
        title="Adopt Redis for hot caching layer",
        rationale="In-memory cache for the hot read paths across the API tier with pub/sub.",
    )
    cloud_result = confirm_decision(cloud, cloud_confirm)

    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    assert _dump(fs_result) == _dump(cloud_result)
    fs_body = fs_store.read_decision(fs_result.decision_id)
    cloud_body = cloud.read_decision(cloud_result.decision_id)
    assert fs_body == cloud_body


def test_confirm_supersede_byte_identical(both_stores):
    fs_store, cloud = both_stores
    fs_stem = _seed_postgres(fs_store)
    cloud_stem = _seed_postgres(cloud)
    assert fs_stem == cloud_stem

    fs_confirm = _seed_pending_supersede(fs_stem)
    fs_result = confirm_decision(fs_store, fs_confirm)
    cloud_confirm = _seed_pending_supersede(cloud_stem)
    cloud_result = confirm_decision(cloud, cloud_confirm)

    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    assert _dump(fs_result) == _dump(cloud_result)

    fs_new = fs_store.read_decision(fs_result.decision_id)
    cloud_new = cloud.read_decision(cloud_result.decision_id)
    assert fs_new == cloud_new
    assert "supersedes" in fs_new

    fs_old = fs_store.read_decision(fs_stem)
    cloud_old = cloud.read_decision(cloud_stem)
    assert fs_old == cloud_old
    assert "status: superseded" in fs_old


def test_confirm_update_byte_identical(both_stores):
    fs_store, cloud = both_stores
    fs_stem = _seed_postgres(fs_store)
    cloud_stem = _seed_postgres(cloud)

    fs_confirm = _seed_pending_update(fs_stem)
    fs_result = confirm_decision(fs_store, fs_confirm)
    cloud_confirm = _seed_pending_update(cloud_stem)
    cloud_result = confirm_decision(cloud, cloud_confirm)

    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    assert _dump(fs_result) == _dump(cloud_result)

    fs_body = fs_store.read_decision(fs_result.decision_id)
    cloud_body = cloud.read_decision(cloud_result.decision_id)
    assert fs_body == cloud_body
    assert "managed-extensions clause" in fs_body
    assert "version: 2" in fs_body


def test_confirm_resolves_questions_byte_identical_open_questions(both_stores):
    fs_store, cloud = both_stores
    seed = "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    fs_store.write_file(OPEN_QUESTIONS_MD, seed)
    cloud.write_file(OPEN_QUESTIONS_MD, seed)

    rationale = (
        "Mature ecosystem with strong JSON support and excellent tooling for the data layer."
    )
    fs_confirm = _seed_pending_add(
        title="Adopt PostgreSQL for the data layer",
        rationale=rationale,
        resolves=["Q1"],
    )
    fs_result = confirm_decision(fs_store, fs_confirm)
    cloud_confirm = _seed_pending_add(
        title="Adopt PostgreSQL for the data layer",
        rationale=rationale,
        resolves=["Q1"],
    )
    cloud_result = confirm_decision(cloud, cloud_confirm)

    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    assert _dump(fs_result) == _dump(cloud_result)
    assert fs_store.read_file(OPEN_QUESTIONS_MD) == cloud.read_file(OPEN_QUESTIONS_MD)
    assert fs_store.read_decision(fs_result.decision_id) == cloud.read_decision(
        cloud_result.decision_id
    )


def test_confirm_unknown_id_envelope_identical(both_stores):
    fs_store, cloud = both_stores
    fs_result = confirm_decision(fs_store, "no-such-id")
    cloud_result = confirm_decision(cloud, "no-such-id")
    assert fs_result.status == "rejected"
    assert cloud_result.status == "rejected"
    assert _dump(fs_result) == _dump(cloud_result)
