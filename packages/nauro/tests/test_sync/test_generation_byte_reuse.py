from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from nauro.store.generation_projection import (
    GenerationProjectionVerificationError,
    verify_generation_projection,
)
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.remote import TransferBoundaryError
from tests.test_sync.test_generation_acquisition import (
    BINDING,
    OTHER_USER_ID,
    USER_ID,
    FakeServer,
    acquire,
)
from tests.test_sync.test_generation_acquisition import _environment as _environment


def refresh(server, prior):
    return acquire_generation_projection(
        BINDING, active_user_id=USER_ID, session=server.session, prior=prior
    )


def test_reuses_only_matching_target_members_and_verifies_complete_result():
    prior = acquire(
        FakeServer({"project.md": b"same", "state.md": b"old", "context/brief.md": b"gone"})
    )
    artifacts = {"project.md": b"same", "state.md": b"new", "decisions/002.md": b"added"}
    server = FakeServer(artifacts)
    server.generation_id = "01K77777777777777777777777"
    result = refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 1, "object": 2}
    assert server.presign_bodies()[0]["paths"] == ["decisions/002.md", "state.md"]
    baseline = FakeServer(artifacts)
    baseline.generation_id = server.generation_id
    assert result == acquire(baseline)
    assert {a.path: a.content for a in result.artifacts} == artifacts


def test_current_manifest_authorizes_reuse_without_object_requests():
    prior = acquire(FakeServer())
    server = FakeServer()
    server.generation_id = "01K77777777777777777777777"
    result = refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 0, "object": 0}
    assert result.target.identity.generation_id == server.generation_id
    assert result.artifacts == prior.artifacts


def test_first_acquisition_downloads_every_artifact():
    server = FakeServer({"state.md": b"state", "project.md": b"project"})
    result = refresh(server, None)
    assert server.counts == {"projection": 1, "presign": 1, "object": 2}
    assert {a.path: a.content for a in result.artifacts} == server.artifacts


@pytest.mark.parametrize(
    "field,value",
    [
        ("installed_for_user_id", OTHER_USER_ID),
        ("projection_scope_id", "b" * 64),
        ("project_id", "01K88888888888888888888888"),
        ("projection_class", "viewer"),
    ],
)
def test_changed_identity_does_not_supply_reusable_bytes(field, value):
    prior = acquire(FakeServer())
    manifest = json.loads(prior.manifest_json)
    if field in manifest:
        manifest[field] = value
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    identity = prior.target.identity.model_copy(
        update={field: value, "manifest_digest": hashlib.sha256(raw).hexdigest()}
    )
    binding = replace(BINDING, project_id=value) if field == "project_id" else BINDING
    prior = verify_generation_projection(
        replace(prior.target, identity=identity, binding=binding),
        manifest_json=raw,
        artifacts=tuple((a.path, a.content) for a in prior.artifacts),
    )
    server = FakeServer()
    assert refresh(server, prior).artifacts == acquire(FakeServer()).artifacts
    assert server.counts == {"projection": 1, "presign": 1, "object": 1}


def test_different_endpoint_does_not_supply_reusable_bytes():
    prior = acquire(FakeServer())
    binding = replace(BINDING, server_url="https://other.test")
    prior = verify_generation_projection(
        replace(prior.target, binding=binding),
        manifest_json=prior.manifest_json,
        artifacts=tuple((a.path, a.content) for a in prior.artifacts),
    )
    server = FakeServer()
    refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 1, "object": 1}


def test_corrupt_saved_bytes_refuse_before_presigning():
    prior = acquire(FakeServer())
    artifact = replace(prior.artifacts[0], content=b"tampered")
    object.__setattr__(prior, "artifacts", (artifact,))
    server = FakeServer()
    with pytest.raises(GenerationProjectionVerificationError):
        refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 0, "object": 0}


def test_changed_download_still_requires_exact_digest():
    prior = acquire(FakeServer())
    server = FakeServer({"project.md": b"new"})
    server.object_hook = lambda *a: httpx.Response(200, content=b"wrong")
    with pytest.raises(GenerationProjectionVerificationError):
        refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 1, "object": 1}


def test_reuse_does_not_bypass_current_authorization():
    prior = acquire(FakeServer())
    server = FakeServer()
    server.projection_hook = lambda *a: httpx.Response(403, json={"error": "forbidden"})
    with pytest.raises(TransferBoundaryError) as error:
        refresh(server, prior)
    assert error.value.status == 403
    assert server.counts == {"projection": 1, "presign": 0, "object": 0}


def test_one_add_to_562_artifacts_downloads_only_the_addition():
    artifacts = {f"decisions/{n:03d}.md": f"# {n}\n".encode() for n in range(562)}
    prior = acquire(FakeServer(artifacts))
    server = FakeServer(artifacts | {"decisions/562.md": b"# Added\n"})
    server.generation_id = "01K77777777777777777777777"
    result = refresh(server, prior)
    assert server.counts == {"projection": 1, "presign": 1, "object": 1}
    assert server.presign_bodies()[0]["paths"] == ["decisions/562.md"]
    assert {a.path: a.content for a in result.artifacts} == server.artifacts
