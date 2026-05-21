"""Layer-3 cross-surface parity for ``check_decision``.

The surface-level parity test (``test_check_decision_parity``) covers all
three local adapters against the same ``FilesystemStore``. This file goes
one layer deeper: the same kernel call against ``FilesystemStore`` and
``mcp_server.store.cloud_store.CloudStore`` must produce the same
:class:`CheckDecisionResult` for the same fixed inputs. That is the
no-drift-by-construction guarantee D170 + D174 commit to.

CloudStore lives in the private mcp-server repo and ships in the paired
cross-repo cutover PR. When this test runs from inside the nauro
workspace alone (CI for the public monorepo), ``mcp_server`` is not
installed and the test skips at module load via ``pytest.importorskip``.
Engineers who have both packages on a single ``PYTHONPATH`` exercise it
locally to catch local-vs-cloud envelope drift before merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cloud_store_module = pytest.importorskip(
    "mcp_server.store.cloud_store",
    reason="CloudStore is provided by the private mcp-server repo.",
)
boto3 = pytest.importorskip("boto3", reason="boto3 needed to seed a moto S3 bucket.")
moto = pytest.importorskip("moto", reason="moto needed for in-memory S3.")

# Imports below ``importorskip`` deliberately run only when both packages are
# installed; ruff E402 does not apply here.
from nauro_core.decision_model import (  # noqa: E402
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import check_decision  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"

PROPOSED_APPROACH = "Migrate primary storage to PostgreSQL"

# Fixed seed: one active decision that should match the proposal, one
# superseded decision that must not surface in the hits. The exact strings
# are mirrored into both stores so retrieval inputs are identical by
# construction; any envelope drift between the two surfaces is then a real
# bug, not a fixture mismatch.
SEED_DECISIONS: dict[str, str] = {
    "001-adopt-postgresql": format_decision(
        Decision(
            num=1,
            title="Adopt PostgreSQL",
            rationale=(
                "Use PostgreSQL for ACID transactional semantics across the "
                "platform; replaces the original document-store plan."
            ),
            confidence=DecisionConfidence.high,
            status=DecisionStatus.active,
        )
    ),
    "002-adopt-mongo": format_decision(
        Decision(
            num=2,
            title="Adopt MongoDB",
            rationale=(
                "Initial document-store choice for flexible schemas, later "
                "superseded by PostgreSQL."
            ),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.superseded,
            superseded_by="1",
        )
    ),
}


def _seed_filesystem_store(root: Path) -> FilesystemStore:
    decisions_dir = root / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for stem, body in SEED_DECISIONS.items():
        (decisions_dir / f"{stem}.md").write_text(body)
    return FilesystemStore(root)


def _seed_cloud_store(s3_client) -> CloudStore:
    prefix = f"users/{TEST_USER_ID}/projects/{TEST_PROJECT_ID}"
    for stem, body in SEED_DECISIONS.items():
        s3_client.put_object(
            Bucket=TEST_BUCKET,
            Key=f"{prefix}/decisions/{stem}.md",
            Body=body.encode(),
        )
    return CloudStore(user_id=TEST_USER_ID, project_id=TEST_PROJECT_ID)


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair seeded with identical decisions.

    The cloud half lives inside a moto-mocked S3 + an env-pointed bucket so
    the test never touches AWS. ``NAURO_S3_BUCKET`` is monkey-patched onto
    the environment because :class:`CloudStore` reads it lazily.
    """
    monkeypatch.setenv("NAURO_S3_BUCKET", TEST_BUCKET)

    with moto.mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=TEST_BUCKET)

        fs_store = _seed_filesystem_store(tmp_path)
        cloud = _seed_cloud_store(s3_client)
        yield fs_store, cloud


def test_check_decision_result_matches_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_result = check_decision(fs_store, PROPOSED_APPROACH)
    cloud_result = check_decision(cloud, PROPOSED_APPROACH)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )


def test_active_decision_surfaces_in_both_stores(both_stores):
    """Pins the contract: only D001 (active) surfaces; D002 (superseded) does not."""
    fs_store, cloud = both_stores

    fs_result = check_decision(fs_store, PROPOSED_APPROACH)
    cloud_result = check_decision(cloud, PROPOSED_APPROACH)

    for result in (fs_result, cloud_result):
        assert result.error is None
        ids = [hit.id for hit in result.related_decisions]
        assert "decision-001" in ids
        assert "decision-002" not in ids


def test_rejection_envelope_matches_across_stores(both_stores):
    """Over-length inputs produce the same ``error`` payload from each surface."""
    from nauro_core.constants import MAX_APPROACH_LENGTH

    fs_store, cloud = both_stores
    overlong = "x" * (MAX_APPROACH_LENGTH + 1)

    fs_result = check_decision(fs_store, overlong)
    cloud_result = check_decision(cloud, overlong)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
    assert fs_result.error is not None
    assert fs_result.error.kind == "rejected"
