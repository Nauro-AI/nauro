"""Layer-3 cross-surface parity for ``get_context``.

The surface-level parity test (``test_get_context_parity``) covers the
local adapters against the same ``FilesystemStore``. This file goes one
layer deeper: the same kernel call against ``FilesystemStore`` and
``mcp_server.store.cloud_store.CloudStore`` must produce the same
:class:`GetContextResult` for the same fixed inputs. That is the
no-drift-by-construction guarantee the operations-kernel doctrine commits
to.

CloudStore is consumed by other transports and is not always installed in
the same environment as ``nauro``. When this test runs from inside the
nauro workspace alone, ``mcp_server`` is unavailable and the test skips
at module load via ``pytest.importorskip``. Engineers who have both
packages on a single ``PYTHONPATH`` exercise it locally to catch
local-vs-cloud envelope drift before merge.
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
from nauro_core.operations import get_context  # noqa: E402

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402

CloudStore = cloud_store_module.CloudStore

TEST_BUCKET = "nauro-cross-surface-test"
TEST_USER_ID = "01TEST" + "0" * 20
TEST_PROJECT_ID = "01TESTPROJECT00000000000"

# Fixed seed: project + state + stack + questions + two active decisions.
# Mirrored byte-for-byte across both stores so any envelope drift is a
# real bug rather than a fixture mismatch.
SEED_FILES: dict[str, str] = {
    "project.md": "# Project\n\nGoal: cross-store parity.\n",
    "state_current.md": "# Current State\n\n- Shipped Auth0 cutover\n",
    "stack.md": "# Stack\n- **Python 3.11** — primary language\n",
    "open-questions.md": "# Open Questions\n- [Q1] Do we add Redis?\n",
}

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
    root.mkdir(parents=True, exist_ok=True)
    for name, body in SEED_FILES.items():
        (root / name).write_text(body)
    decisions_dir = root / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for stem, body in SEED_DECISIONS.items():
        (decisions_dir / f"{stem}.md").write_text(body)
    return FilesystemStore(root)


def _seed_cloud_store(s3_client) -> CloudStore:
    prefix = f"users/{TEST_USER_ID}/projects/{TEST_PROJECT_ID}"
    for name, body in SEED_FILES.items():
        s3_client.put_object(Bucket=TEST_BUCKET, Key=f"{prefix}/{name}", Body=body.encode())
    for stem, body in SEED_DECISIONS.items():
        s3_client.put_object(
            Bucket=TEST_BUCKET,
            Key=f"{prefix}/decisions/{stem}.md",
            Body=body.encode(),
        )
    return CloudStore(user_id=TEST_USER_ID, project_id=TEST_PROJECT_ID)


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair seeded with identical content."""
    monkeypatch.setenv("NAURO_S3_BUCKET", TEST_BUCKET)

    with moto.mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=TEST_BUCKET)

        fs_store = _seed_filesystem_store(tmp_path)
        cloud = _seed_cloud_store(s3_client)
        yield fs_store, cloud


@pytest.mark.parametrize("level", [0, 1, 2])
def test_get_context_result_matches_across_stores(both_stores, level):
    fs_store, cloud = both_stores

    fs_result = get_context(fs_store, level)
    cloud_result = get_context(cloud, level)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
