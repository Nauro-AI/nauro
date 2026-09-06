from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from nauro.store import generation_installation as installation
from nauro.store import generation_read as reader
from nauro.store.generation_authority import RefreshRequiredError, ReplicaActorMismatchError
from nauro.store.generation_read import GenerationReadError, read_installed_generation
from tests.test_generation_installation import (
    ARTIFACTS,
    OTHER_STATE_ID,
    SCOPE_ID,
    USER_ID,
    _actor,
    _projection,
)


@pytest.fixture
def installed(monkeypatch):
    projection = _projection()
    projection.target.binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    root = installation.install_generation_root(projection)
    authority = installation.publish_generation_control(root)
    return projection, root, authority


def _read(projection, **kwargs):
    return read_installed_generation(
        projection.target.binding,
        active_user_id=kwargs.get("user", USER_ID),
        active_projection_scope_id=kwargs.get("scope", SCOPE_ID),
        timeout=0,
    )


def test_capture_preserves_exact_bytes_after_disk_and_pointer_changes(installed):
    projection, root, authority = installed
    captured = _read(projection)
    assert captured.target == projection.target
    assert captured.manifest_json == projection.manifest_json
    assert {a.path: a.content for a in captured.artifacts} == ARTIFACTS
    (root.root_path / "store/decisions/001-x.md").write_bytes(b"changed")
    pointer = authority.pointer.model_copy(update={"installed_state_id": OTHER_STATE_ID})
    (_actor(projection.target.binding.store_path) / "pointer.json").write_bytes(
        pointer.canonical_bytes()
    )
    assert captured.artifacts_by_path["decisions/001-x.md"].content == b"# 001\n"
    with pytest.raises(TypeError):
        captured.artifacts_by_path["extra"] = captured.artifacts[0]


@pytest.mark.parametrize("defect", ["missing", "corrupt", "extra", "directory", "hardlink"])
def test_capture_refuses_invalid_root(installed, defect, tmp_path):
    projection, root, _ = installed
    artifact = root.root_path / "store/decisions/001-x.md"
    if defect == "missing":
        artifact.unlink()
    elif defect == "corrupt":
        artifact.write_bytes(b"wrong")
    elif defect == "extra":
        (root.root_path / "store/extra.md").write_bytes(b"extra")
    elif defect == "directory":
        artifact.unlink()
        artifact.mkdir()
    else:
        (tmp_path / "linked").hardlink_to(artifact)
    with pytest.raises(GenerationReadError) as raised:
        _read(projection)
    assert raised.value.code == "generation_read_unavailable"


@pytest.mark.parametrize(
    "limit", ["_MAX_MANIFEST_BYTES", "_MAX_ARTIFACT_BYTES", "_MAX_CAPTURE_BYTES"]
)
def test_capture_refuses_oversized_content(installed, monkeypatch, limit):
    projection, _, _ = installed
    monkeypatch.setattr(reader, limit, 1)
    with pytest.raises(GenerationReadError, match="capture limit"):
        _read(projection)


def test_capture_refuses_pointer_change_before_return(installed, monkeypatch):
    projection, _, authority = installed
    original = reader._capture

    def capture(selected):
        result = original(selected)
        pointer = authority.pointer.model_copy(update={"installed_state_id": OTHER_STATE_ID})
        (_actor(projection.target.binding.store_path) / "pointer.json").write_bytes(
            pointer.canonical_bytes()
        )
        return result

    monkeypatch.setattr(reader, "_capture", capture)
    with pytest.raises(GenerationReadError, match="changed during capture"):
        _read(projection)


def test_capture_keeps_actor_and_scope_refusals_distinct(installed):
    projection, _, _ = installed
    with pytest.raises(ReplicaActorMismatchError):
        _read(projection, user=None)
    with pytest.raises(RefreshRequiredError):
        _read(projection, scope="b" * 64)


def test_capture_refuses_flat_authority(installed):
    projection, _, _ = installed
    with pytest.raises(GenerationReadError, match="no installed generation"):
        read_installed_generation(
            replace(projection.target.binding, mode="local", server_url=None),
            active_user_id=USER_ID,
            active_projection_scope_id=SCOPE_ID,
        )


def test_capture_has_only_the_dormant_store_consumer():
    root = Path(reader.__file__).parents[1]
    consumers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.store.generation_read":
                consumers.append(path.relative_to(root).as_posix())
    assert consumers == ["store/generation_store.py"]


def test_capture_refuses_a_replaced_file(installed, monkeypatch):
    projection, root, _ = installed
    original = reader._read_expected
    artifact = root.root_path / "store/decisions/001-x.md"

    def read(path, length, identity, display):
        result = original(path, length, identity, display)
        if path == artifact:
            replacement = artifact.with_suffix(".replacement")
            replacement.write_bytes(result)
            replacement.replace(artifact)
        return result

    monkeypatch.setattr(reader, "_read_expected", read)
    with pytest.raises(GenerationReadError, match="changed during capture"):
        _read(projection)


def test_capture_holds_control_lock_through_verification(installed, monkeypatch):
    from nauro.store.replica_control import ReplicaControlBusyError, locked_replica_control_snapshot

    projection, _, _ = installed
    original = reader._capture

    def capture(authority):
        with pytest.raises(ReplicaControlBusyError):
            with locked_replica_control_snapshot(
                authority.binding,
                active_user_id=USER_ID,
                active_projection_scope_id=SCOPE_ID,
                timeout=0,
            ):
                pass
        return original(authority)

    monkeypatch.setattr(reader, "_capture", capture)
    assert _read(projection).manifest_json == projection.manifest_json
    with locked_replica_control_snapshot(
        projection.target.binding,
        active_user_id=USER_ID,
        active_projection_scope_id=SCOPE_ID,
        timeout=0,
    ) as snapshot:
        assert snapshot.authority.pointer.generation_id == projection.target.identity.generation_id


def test_capture_refuses_linked_artifact(installed, tmp_path):
    from nauro.store.replica_control import ReplicaControlReadError

    projection, root, _ = installed
    artifact = root.root_path / "store/decisions/001-x.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        artifact.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit test symlinks")
    with pytest.raises(ReplicaControlReadError) as raised:
        _read(projection)
    assert raised.value.code == "generation_control_unavailable"
