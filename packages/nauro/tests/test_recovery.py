from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nauro.store import recovery
from nauro.store.recovery import RecoveryError, bind_local_store, restore_cloud_store
from nauro.store.registry import get_project_v2, get_store_path_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store

PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _store_files(root: Path) -> dict[str, bytes]:
    scaffold_project_store("nauro", root)
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _mock_remote(monkeypatch, files: dict[str, bytes]) -> None:
    manifest = [
        {
            "path": path,
            "etag": f'"{hashlib.md5(content).hexdigest()}"',
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(files.items())
    ]
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: manifest)
    monkeypatch.setattr(
        recovery,
        "request_presigned_urls",
        lambda _pid, operations: [
            {"verb": "GET", "path": op["path"], "url": f"memory://{op['path']}"}
            for op in operations
        ],
    )
    monkeypatch.setattr(
        recovery,
        "fetch_via_presigned_url",
        lambda url: files[url.removeprefix("memory://")],
    )


def test_bind_local_store_registers_repo_without_copying_store(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(repo, {"mode": "local", "id": PID, "name": "nauro"})
    store = tmp_path / "external" / PID
    scaffold_project_store("nauro", store)

    result = bind_local_store(repo, store)

    assert result.store_path == store
    assert get_project_v2(PID)["store_path"] == str(store)
    assert not get_store_path_v2(PID).exists()


def test_restore_cloud_store_installs_complete_record_atomically(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    destination = tmp_path / "projects" / PID

    restored = restore_cloud_store(PID, destination)

    assert restored == destination
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == files
    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_restore_cloud_store_refuses_nonempty_destination_before_network(tmp_path, monkeypatch):
    destination = tmp_path / PID
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")

    def explode(_pid):
        raise AssertionError("network must not run")

    monkeypatch.setattr(recovery, "fetch_manifest", explode)

    with pytest.raises(RecoveryError, match="nonempty"):
        restore_cloud_store(PID, destination)

    assert (destination / "keep.txt").read_text() == "keep"


def test_restore_cloud_store_cleans_staging_on_hash_mismatch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    manifest = recovery.fetch_manifest(PID)
    manifest[0]["sha256"] = "0" * 64
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: manifest)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="hash"):
        restore_cloud_store(PID, destination)

    assert not destination.exists()
    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_restore_cloud_store_skips_hash_check_for_opaque_etag(tmp_path, monkeypatch):
    """Multipart and SSE-KMS ETags are not content MD5s.

    Restore must fall back to the size and sha256 checks for such entries
    instead of failing a record the sync pull path would have accepted.
    """
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    manifest = recovery.fetch_manifest(PID)
    manifest[0]["etag"] = '"0123456789abcdef0123456789abcdef-2"'
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: manifest)
    destination = tmp_path / "projects" / PID

    result = restore_cloud_store(PID, destination)

    assert result == destination
    restored = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert restored == files


def test_restore_cloud_store_still_catches_corruption_behind_opaque_etag(tmp_path, monkeypatch):
    """An opaque ETag skips only the MD5 comparison — the sha256 check still
    rejects corrupted content for that entry."""
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    manifest = recovery.fetch_manifest(PID)
    manifest[0]["etag"] = '"0123456789abcdef0123456789abcdef-2"'
    manifest[0]["sha256"] = "0" * 64
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: manifest)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="hash"):
        restore_cloud_store(PID, destination)

    assert not destination.exists()
    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_restore_accepts_default_store_path_under_symlinked_home(tmp_path, monkeypatch):
    """The default home path is exempt from the canonical-destination rule.

    NAURO_HOME legitimately traverses a symlink on macOS ($TMPDIR) and
    similar hosts; first connection and restore must not refuse the derived
    default path there. Reaching the empty-manifest error proves the
    destination guard passed.
    """
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(linked_home))
    destination = get_store_path_v2(PID)
    assert destination.resolve(strict=False) != destination
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: [])

    with pytest.raises(recovery.EmptyCloudRecordError):
        restore_cloud_store(PID, destination)


def test_restore_rejects_non_canonical_external_destination(tmp_path, monkeypatch):
    """External destinations keep the canonical rule so the registry never
    records a symlink-relative location."""
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    destination = linked_dir / "elsewhere" / PID

    with pytest.raises(RecoveryError, match="canonical"):
        restore_cloud_store(PID, destination)


def test_restore_installs_into_preexisting_empty_destination(tmp_path, monkeypatch):
    """A leftover empty destination directory (e.g. from an aborted attach)
    must not fail the final install step."""
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    destination = tmp_path / "projects" / PID
    destination.mkdir(parents=True)

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert (destination / "project.md").is_file()


@pytest.mark.parametrize("path", ["../escape", "/absolute"])
def test_restore_cloud_store_refuses_unsafe_manifest_without_partial_record(
    path, tmp_path, monkeypatch
):
    manifest = [{"path": path, "etag": "e", "size": 1}]
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: manifest)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError):
        restore_cloud_store(PID, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "project.md", "etag": "0" * 32, "size": "1"},
        {"path": "project.md", "etag": 42, "size": 1},
        {"path": 42, "etag": "0" * 32, "size": 1},
    ],
)
def test_restore_cloud_store_rejects_malformed_manifest_types(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "fetch_manifest", lambda _pid: [entry])
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="manifest"):
        restore_cloud_store(PID, destination)

    assert not destination.exists()
    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_restore_cloud_store_rejects_malformed_presign_result(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = _store_files(source)
    _mock_remote(monkeypatch, files)
    monkeypatch.setattr(
        recovery,
        "request_presigned_urls",
        lambda _pid, operations: [{"verb": "GET", "path": operations[0]["path"], "url": 42}],
    )
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="presign"):
        restore_cloud_store(PID, destination)

    assert not destination.exists()
    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_restore_cloud_store_rejects_damaged_decision_graph(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = _store_files(source)
    decision = next(path for path in files if path.startswith("decisions/"))
    files[decision] = files[decision].replace(b"superseded_by: null", b"superseded_by: '999'")
    _mock_remote(monkeypatch, files)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="integrity"):
        restore_cloud_store(PID, destination)

    assert not destination.exists()
