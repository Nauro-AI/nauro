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

import pytest

cloud_store_module = pytest.importorskip(
    "mcp_server.store.cloud_store",
    reason="CloudStore is provided by the private mcp-server repo.",
)
pytest.importorskip("boto3", reason="boto3 needed to seed a moto S3 bucket.")
pytest.importorskip("moto", reason="moto needed for in-memory S3.")

# Imports below ``importorskip`` deliberately run only when both packages are
# installed; ruff E402 does not apply here.
from nauro_core.decision_model import (  # noqa: E402
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import get_context  # noqa: E402

from tests.conftest import moto_s3_bucket  # noqa: E402
from tests.cross_surface.conftest import (  # noqa: E402
    seed_cloud_store,
    seed_filesystem_store,
)

CloudStore = cloud_store_module.CloudStore

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


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair seeded with identical content."""
    with moto_s3_bucket(monkeypatch) as s3_client:
        fs_store = seed_filesystem_store(tmp_path, SEED_DECISIONS, SEED_FILES)
        cloud = seed_cloud_store(s3_client, SEED_DECISIONS, SEED_FILES)
        yield fs_store, cloud


@pytest.mark.parametrize("level", [0, 1, 2])
def test_get_context_result_matches_across_stores(both_stores, level):
    fs_store, cloud = both_stores

    fs_result = get_context(fs_store, level)
    cloud_result = get_context(cloud, level)

    assert fs_result.model_dump(mode="json", exclude_none=True) == cloud_result.model_dump(
        mode="json", exclude_none=True
    )
