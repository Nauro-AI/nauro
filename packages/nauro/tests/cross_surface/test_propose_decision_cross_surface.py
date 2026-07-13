"""Cross-store parity for ``propose_decision``.

The surface-level parity test (``test_propose_decision_parity``) covers
the local adapters against the same ``FilesystemStore``. This file goes
one layer deeper: the same kernel call against an identically-seeded
store must produce the same :class:`ProposeDecisionResult` regardless of
which ``Store`` implementation is passed in.

The kernel writes through :meth:`Store.write_file` and reads through
:meth:`Store.list_decisions` / :meth:`Store.read_decision` / :meth:`Store.read_file`.
All four operations sit on the locked Store protocol, so a
``FilesystemStore`` and a ``CloudStore`` seeded with the same initial
content must produce byte-identical decision files (for the add and
supersede paths) and byte-identical updated ``open-questions.md``
(for the ``resolves_questions`` path).

``CloudStore`` is consumed by other transports and is not always
installed alongside ``nauro``. When this test runs from inside the
nauro workspace alone, the module skips at load via
``pytest.importorskip``.
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
from nauro_core.constants import OPEN_QUESTIONS_MD  # noqa: E402
from nauro_core.operations import propose_decision  # noqa: E402
from nauro_core.operations.propose_decision import (  # noqa: E402
    _execute_operation,
    _write_decision_direct,
)

from nauro.store.filesystem_store import FilesystemStore  # noqa: E402
from tests.conftest import (  # noqa: E402
    CROSS_SURFACE_PROJECT_ID,
    CROSS_SURFACE_USER_ID,
    moto_s3_bucket,
)
from tests.cross_surface.conftest import _dump  # noqa: E402

CloudStore = cloud_store_module.CloudStore


@pytest.fixture
def both_stores(tmp_path, monkeypatch):
    """Yield a (FilesystemStore, CloudStore) pair backed by separate roots."""
    with moto_s3_bucket(monkeypatch):
        fs_store = FilesystemStore(tmp_path)
        cloud = CloudStore(user_id=CROSS_SURFACE_USER_ID, project_id=CROSS_SURFACE_PROJECT_ID)
        yield fs_store, cloud


def _seed_postgres(store) -> str:
    """Write a parseable Postgres decision and return its file stem."""
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


def test_add_confirmed_decision_file_byte_identical(both_stores):
    """An auto-confirmed add must produce byte-identical decision files
    and identical kernel envelopes across the two stores."""
    fs_store, cloud = both_stores

    fs_result = propose_decision(
        fs_store,
        title="Adopt Redis for hot caching layer",
        rationale="In-memory cache for the hot read paths across the API tier with pub/sub.",
        confidence="high",
    )
    cloud_result = propose_decision(
        cloud,
        title="Adopt Redis for hot caching layer",
        rationale="In-memory cache for the hot read paths across the API tier with pub/sub.",
        confidence="high",
    )

    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    # Envelope identity: decision_id and side-channel fields round-trip the same.
    assert _dump(fs_result) == _dump(cloud_result)
    # Byte-identical decision file.
    fs_body = fs_store.read_decision(fs_result.decision_id)
    cloud_body = cloud.read_decision(cloud_result.decision_id)
    assert fs_body == cloud_body


def test_supersede_byte_identical_new_and_flipped_old(both_stores):
    """A supersede write must produce byte-identical new decision AND
    byte-identical flipped old frontmatter across the two stores."""
    fs_store, cloud = both_stores
    fs_stem = _seed_postgres(fs_store)
    cloud_stem = _seed_postgres(cloud)
    assert fs_stem == cloud_stem

    # Drive the supersede through the kernel's private execute path so
    # the test is deterministic — the kernel write path is what we are
    # pinning here, independent of Tier 2 advisory similarity outcomes.
    proposal = {
        "title": "Switch to managed PostgreSQL provider",
        "rationale": "Reduces operational burden; the self-hosting rationale no longer applies.",
        "confidence": "high",
    }
    fs_decision_id, _, _, _, fs_err = _execute_operation(fs_store, "supersede", proposal, fs_stem)
    cloud_decision_id, _, _, _, cloud_err = _execute_operation(
        cloud, "supersede", proposal, cloud_stem
    )
    assert fs_err is None
    assert cloud_err is None
    assert fs_decision_id == cloud_decision_id

    fs_new_body = fs_store.read_decision(fs_decision_id)
    cloud_new_body = cloud.read_decision(cloud_decision_id)
    assert fs_new_body == cloud_new_body
    assert "supersedes" in fs_new_body

    fs_old_body = fs_store.read_decision(fs_stem)
    cloud_old_body = cloud.read_decision(cloud_stem)
    assert fs_old_body == cloud_old_body
    assert "status: superseded" in fs_old_body


def test_update_byte_identical_appended_rationale(both_stores):
    """An update write appends a dated rationale paragraph; the resulting
    body must be byte-identical across the two stores."""
    fs_store, cloud = both_stores
    fs_stem = _seed_postgres(fs_store)
    cloud_stem = _seed_postgres(cloud)

    proposal = {
        "title": "",
        "rationale": "Adds a managed-extensions clause to the existing PostgreSQL choice.",
    }
    fs_id, _, _, _, fs_err = _execute_operation(fs_store, "update", proposal, fs_stem)
    cloud_id, _, _, _, cloud_err = _execute_operation(cloud, "update", proposal, cloud_stem)
    assert fs_err is None
    assert cloud_err is None
    assert fs_id == cloud_id

    fs_body = fs_store.read_decision(fs_id)
    cloud_body = cloud.read_decision(cloud_id)
    assert fs_body == cloud_body
    assert "managed-extensions clause" in fs_body
    assert "version: 2" in fs_body


def test_resolves_questions_byte_identical_open_questions(both_stores):
    """A confirmed add with ``resolves_questions`` updates
    ``open-questions.md``; the file must be byte-identical across the
    two stores."""
    fs_store, cloud = both_stores
    seed = "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    fs_store.write_file(OPEN_QUESTIONS_MD, seed)
    cloud.write_file(OPEN_QUESTIONS_MD, seed)

    rationale = (
        "Mature ecosystem with strong JSON support and excellent tooling for the data layer."
    )
    fs_result = propose_decision(
        fs_store,
        title="Adopt PostgreSQL for the data layer",
        rationale=rationale,
        confidence="high",
        resolves_questions=["Q1"],
    )
    cloud_result = propose_decision(
        cloud,
        title="Adopt PostgreSQL for the data layer",
        rationale=rationale,
        confidence="high",
        resolves_questions=["Q1"],
    )
    assert fs_result.status == "confirmed"
    assert cloud_result.status == "confirmed"
    assert _dump(fs_result) == _dump(cloud_result)
    # The kernel rewrote open-questions.md to flag Q1 as resolved. Files must match.
    assert fs_store.read_file(OPEN_QUESTIONS_MD) == cloud.read_file(OPEN_QUESTIONS_MD)
    # And the decision file body matches too.
    assert fs_store.read_decision(fs_result.decision_id) == cloud.read_decision(
        cloud_result.decision_id
    )
