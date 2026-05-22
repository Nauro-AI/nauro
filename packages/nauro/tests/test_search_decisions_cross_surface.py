"""Layer-3 cross-surface parity for ``search_decisions``.

The surface-level parity test (``test_search_decisions_parity``) covers
the local adapters against the same ``FilesystemStore``. This file goes
one layer deeper: the same kernel call against ``FilesystemStore`` and
``mcp_server.store.cloud_store.CloudStore`` must produce the same
:class:`SearchDecisionsResult` for the same fixed inputs. That is the
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
    reason="CloudStore is consumed by other transports; not always installed alongside nauro.",
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
from nauro_core.operations import search_decisions  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"

# Fixed seed: two active decisions whose titles + rationale make BM25
# scoring deterministic. The same strings are mirrored into both stores
# so the input envelope is identical by construction; any envelope drift
# between the two surfaces is then a real bug, not a fixture mismatch.
SEED_DECISIONS: dict[str, str] = {
    "001-use-auth0-for-authentication": format_decision(
        Decision(
            num=1,
            title="Use Auth0 for authentication",
            rationale=(
                "Auth0 provides OAuth 2.1 support and handles JWT validation across the platform."
            ),
            confidence=DecisionConfidence.high,
            status=DecisionStatus.active,
        )
    ),
    "002-use-fastapi-for-mcp-server": format_decision(
        Decision(
            num=2,
            title="Use FastAPI for MCP server",
            rationale=("FastAPI plus Mangum is the canonical Lambda deployment combination."),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
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


def test_search_decisions_result_matches_across_stores(both_stores):
    fs_store, cloud = both_stores

    fs_result = search_decisions(fs_store, "Auth0", limit=10)
    cloud_result = search_decisions(cloud, "Auth0", limit=10)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )


def test_search_decisions_result_matches_across_stores_multi_word(both_stores):
    fs_store, cloud = both_stores

    fs_result = search_decisions(fs_store, "FastAPI Lambda deployment", limit=10)
    cloud_result = search_decisions(cloud, "FastAPI Lambda deployment", limit=10)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
