"""Layer-3 cross-surface parity for ``check_decision``.

The surface-level parity test (``test_check_decision_parity``) covers all
three local adapters against the same ``FilesystemStore``. This file goes
one layer deeper: the same kernel call against ``FilesystemStore`` and
``mcp_server.store.cloud_store.CloudStore`` must produce the same
:class:`CheckDecisionResult` for the same fixed inputs. That is the
no-drift-by-construction guarantee: both surfaces run the same operations
kernel, so identical inputs must yield identical results.

CloudStore lives in the private mcp-server repo and ships in the paired
cross-repo cutover PR. When this test runs from inside the nauro
workspace alone (CI for the public monorepo), ``mcp_server`` is not
installed and the test skips at module load via ``pytest.importorskip``.
Engineers who have both packages on a single ``PYTHONPATH`` exercise it
locally to catch local-vs-cloud envelope drift before merge.
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
from nauro_core.operations import check_decision  # noqa: E402

from tests.conftest import moto_s3_bucket  # noqa: E402
from tests.cross_surface.conftest import (  # noqa: E402
    seed_cloud_store,
    seed_filesystem_store,
)

CloudStore = cloud_store_module.CloudStore

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


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair seeded with identical decisions.

    The cloud half lives inside a moto-mocked S3 + an env-pointed bucket so
    the test never touches AWS.
    """
    with moto_s3_bucket(monkeypatch) as s3_client:
        fs_store = seed_filesystem_store(tmp_path, SEED_DECISIONS)
        cloud = seed_cloud_store(s3_client, SEED_DECISIONS)
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


def _envelope(store_label: str, result) -> dict:
    """Mirror the adapter envelope construction used by tool wrappers."""
    return {"store": store_label, **result.model_dump(mode="json", exclude_none=True)}


def test_envelope_byte_identical_modulo_store_field(both_stores):
    """Envelopes are byte-identical once the store discriminator is substituted.

    Pins the wire-shape guarantee: once the ``"store"`` field is normalised,
    the JSON-serialised envelope from FilesystemStore is byte-equal to the
    one from CloudStore. Any field-ordering, default-elision, or coercion
    drift between the two surfaces shows up here, even when the underlying
    ``CheckDecisionResult`` model dumps still compare equal as dicts.
    """
    fs_store, cloud = both_stores

    fs_result = check_decision(fs_store, PROPOSED_APPROACH)
    cloud_result = check_decision(cloud, PROPOSED_APPROACH)

    # Happy path: similar_decisions is non-empty because D001 matches.
    assert fs_result.related_decisions, "fixture should surface decision-001"

    local_envelope = _envelope("local", fs_result)
    remote_envelope = _envelope("remote", cloud_result)
    remote_envelope["store"] = "local"

    import json as _json

    local_bytes = _json.dumps(local_envelope, sort_keys=True).encode()
    remote_bytes = _json.dumps(remote_envelope, sort_keys=True).encode()
    assert local_bytes == remote_bytes


def test_envelope_byte_identical_empty_result_branch(both_stores):
    """Empty-result envelopes also stay byte-identical across surfaces."""
    fs_store, cloud = both_stores

    # An approach with no overlap with the seeded decisions; retrieval
    # returns no hits from either store.
    no_match_approach = "Adopt a fictional widget library for sprocket rendering"

    fs_result = check_decision(fs_store, no_match_approach)
    cloud_result = check_decision(cloud, no_match_approach)

    assert fs_result.related_decisions == []
    assert cloud_result.related_decisions == []

    local_envelope = _envelope("local", fs_result)
    remote_envelope = _envelope("remote", cloud_result)
    remote_envelope["store"] = "local"

    import json as _json

    local_bytes = _json.dumps(local_envelope, sort_keys=True).encode()
    remote_bytes = _json.dumps(remote_envelope, sort_keys=True).encode()
    assert local_bytes == remote_bytes
