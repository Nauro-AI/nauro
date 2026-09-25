from __future__ import annotations

import hashlib
import json
import shutil

import httpx
import pytest

from nauro.store.migration_admission import (
    MigrationAdmissionError,
    inspect_migration,
    require_migration_admission,
)
from nauro.store.read_authority import observe_generation_marker, require_legacy_context
from nauro.store.resolution import resolve_project_binding
from nauro.sync import migration_installation as migration
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_refresh import admit_generation_store
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.migration_admission import load_migration_plan
from tests.test_migration_preservation import saved as _saved


@pytest.fixture
def saved(tmp_path, monkeypatch):
    yield from _saved.__wrapped__(tmp_path, monkeypatch)


def run(saved, record=None):
    original, plan, session, *_ = saved
    return migration.continue_migration_installation(
        record or original, plan.assessment.projection, session
    )


def test_conversion_preserves_legacy_and_admits_normal_replica(saved):
    record, plan, session, *_ = saved
    source = session.binding.store_path
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    completed = run(saved)
    assert completed.phase == "completed"
    assert completed.migration_id == record.migration_id
    retained = migration.retained_source(completed)
    assert {
        p.relative_to(retained): p.read_bytes() for p in retained.rglob("*") if p.is_file()
    } == before
    assert (plan.backup_root / "plan.json").read_bytes() == plan.manifest_json
    assert not (source / "state_current.md").exists()
    assert observe_generation_marker(session.binding) is not None
    with pytest.raises(PermissionError):
        require_legacy_context(source)
    binding = resolve_project_binding(record.project_id, None, use_cwd=False)
    with GenerationTransferSession(binding, session.client) as ordinary:
        store = admit_generation_store(binding, actor=record.actor, session=ordinary)
        assert store.read_file("state_current.md") == "# Server state\n"
    assert run(saved, completed) == completed


@pytest.mark.parametrize(
    "stage", ["before_rename", "after_rename", "before_install", "after_controls", "final_barrier"]
)
def test_explicit_restart_resumes_same_conversion(saved, monkeypatch, stage):
    original, plan, session, *_ = saved
    rename, install, publish, advance = (
        migration.os.rename,
        migration.install_generation_root,
        migration.publish_generation_control,
        migration._advance,
    )

    def rename_fault(source, target):
        if stage == "after_rename":
            rename(source, target)
        raise OSError("rename interruption")

    def install_fault(*args, **kwargs):
        raise OSError("install interruption")

    def publish_fault(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("controls interruption")

    def advance_fault(record, raw, phase):
        if phase == "completed":
            raise OSError("completion barrier")
        return advance(record, raw, phase)

    if stage in {"before_rename", "after_rename"}:
        monkeypatch.setattr(migration.os, "rename", rename_fault)
    elif stage == "before_install":
        monkeypatch.setattr(migration, "install_generation_root", install_fault)
    elif stage == "after_controls":
        monkeypatch.setattr(migration, "publish_generation_control", publish_fault)
    else:
        monkeypatch.setattr(migration, "_advance", advance_fault)
    with pytest.raises(OSError):
        run(saved)
    current, raw = load_migration_plan(session.binding.store_path)
    assert current.migration_id == original.migration_id
    assert raw == plan.manifest_json
    assert current.phase == (
        "replacing" if stage in {"before_rename", "after_rename"} else "installing"
    )
    with pytest.raises(MigrationAdmissionError, match="incomplete"):
        require_migration_admission(session.binding.store_path)
    monkeypatch.setattr(migration.os, "rename", rename)
    monkeypatch.setattr(migration, "install_generation_root", install)
    monkeypatch.setattr(migration, "publish_generation_control", publish)
    monkeypatch.setattr(migration, "_advance", advance)
    saved = (
        *saved[:2],
        InitialAttachmentSession(session.binding, session.repo, session.connection, session.client),
        *saved[3:],
    )
    assert run(saved, current).phase == "completed"


def test_changed_retained_source_refuses_without_rollback(saved, monkeypatch):
    _, _, session, *_ = saved
    install = migration._install
    monkeypatch.setattr(
        migration, "_install", lambda *a: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    retained = migration.retained_source(current)
    (retained / "state_current.md").write_text("Changed evidence")
    monkeypatch.setattr(migration, "_install", install)
    with pytest.raises(MigrationAdmissionError, match="fresh assessment"):
        run(saved, current)
    assert (retained / "state_current.md").read_text() == "Changed evidence"
    assert inspect_migration(session.binding.store_path) == current


def test_both_source_locations_refuse_before_rename(saved, monkeypatch):
    record, _, session, *_ = saved
    rename = migration.os.rename
    monkeypatch.setattr(
        migration.os, "rename", lambda *a: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    shutil.copytree(session.binding.store_path, migration.retained_source(record))
    monkeypatch.setattr(migration.os, "rename", rename)
    with pytest.raises(MigrationAdmissionError, match="Both source locations exist"):
        run(saved, current)
    assert session.binding.store_path.is_dir()
    assert migration.retained_source(record).is_dir()


def test_completed_record_never_reopens_legacy_when_marker_is_lost(saved):
    _, _, session, *_ = saved
    completed = run(saved)
    marker = session.binding.store_path / ".replica/authority.json"
    marker.unlink()
    with pytest.raises(MigrationAdmissionError, match="lost generation authority"):
        require_migration_admission(session.binding.store_path)
    assert inspect_migration(session.binding.store_path) == completed


def test_revoked_access_after_relocation_keeps_block_and_evidence(saved, monkeypatch):
    _, _, session, control, *_ = saved
    advance = migration._advance

    def revoke(record, raw, phase):
        result = advance(record, raw, phase)
        if phase == "installing":
            control["role"] = "viewer"
        return result

    monkeypatch.setattr(migration, "_advance", revoke)
    with pytest.raises(ValueError, match="owner access"):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    assert current.phase == "installing"
    assert migration.retained_source(current).is_dir()
    with pytest.raises(MigrationAdmissionError, match="incomplete"):
        observe_generation_marker(session.binding)


@pytest.mark.parametrize("phase", ["replacing", "installing"])
def test_visible_phase_must_be_durable_before_restart_mutates(saved, monkeypatch, phase):
    _, _, session, *_ = saved
    advance = migration._advance

    def stop_after_record(record, raw, desired):
        result = advance(record, raw, desired)
        if desired == phase:
            raise OSError("response lost after record replacement")
        return result

    monkeypatch.setattr(migration, "_advance", stop_after_record)
    with pytest.raises(OSError):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    assert current.phase == phase
    monkeypatch.setattr(migration, "_advance", advance)
    sync = migration.sync_file

    def failed_barrier(paths, path):
        if path == migration.admission_path(session.binding.store_path):
            raise OSError("saved phase barrier failed")
        sync(paths, path)

    monkeypatch.setattr(migration, "sync_file", failed_barrier)
    with pytest.raises(OSError, match="saved phase barrier failed"):
        run(saved, current)
    assert session.binding.store_path.exists() is (phase == "replacing")
    assert migration.retained_source(current).exists() is (phase == "installing")
    monkeypatch.setattr(migration, "sync_file", sync)
    assert run(saved, current).phase == "completed"


def test_completed_reopen_accepts_later_legitimate_refresh(saved):
    from nauro.store.generation_projection import (
        GenerationProjectionTarget,
        verify_generation_projection,
    )
    from nauro.sync.generation_refresh import _prepare, commit_generation_refresh

    record, plan, session, control, *_ = saved
    completed = run(saved)
    original = plan.assessment.projection
    manifest = json.loads(original.manifest_json)
    manifest["generation_id"] = "01K55555555555555555555555"
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    identity = original.target.identity.model_copy(
        update={
            "generation_id": manifest["generation_id"],
            "manifest_digest": hashlib.sha256(raw).hexdigest(),
        }
    )
    next_projection = verify_generation_projection(
        GenerationProjectionTarget(session.binding, identity),
        manifest_json=raw,
        artifacts=tuple((a.path, a.content) for a in original.artifacts),
    )
    control["identity"] = identity.model_dump()
    control["manifest"] = raw
    prepared = _prepare(
        session.binding, record.actor, session, bootstrap=False, acquired=next_projection
    )
    commit_generation_refresh(prepared, session=session)
    assert run(saved, completed) == completed


def test_normal_cli_read_after_conversion(saved, monkeypatch):
    from typer.testing import CliRunner

    from nauro.cli.main import app

    record, plan, session, *_ = saved
    run(saved)
    client_type = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client_type(transport=session.client._transport, **kw)
    )
    result = CliRunner().invoke(app, ["get-raw-file", "state_current.md", "--project", "Synthetic"])
    assert result.exit_code == 0, result.output
    assert (
        json.loads(result.stdout)["read_authority"]["generation_id"]
        == plan.assessment.projection.target.identity.generation_id
    )
    assert "# Server state" in result.stdout


def test_installed_root_parent_barrier_failure_keeps_conversion_blocked(saved, monkeypatch):
    _, _, session, *_ = saved
    original = migration.sync_parents

    def fail_parent(paths, path):
        if path == session.binding.store_path:
            raise OSError("new root parent barrier")
        original(paths, path)

    monkeypatch.setattr(migration, "sync_parents", fail_parent)
    with pytest.raises(OSError, match="new root parent barrier"):
        run(saved)
    current, raw = load_migration_plan(session.binding.store_path)
    assert current.phase == "installing"
    assert (session.binding.store_path / ".replica/authority.json").is_file()
    with pytest.raises(MigrationAdmissionError, match="incomplete"):
        observe_generation_marker(session.binding)
    monkeypatch.setattr(migration, "sync_parents", original)
    assert run(saved, current).phase == "completed"


def test_unknown_staging_refuses_without_cleanup_or_execution(saved, monkeypatch):
    _, _, session, *_ = saved
    original = migration.install_generation_root

    def stop_after_root(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("root staged")

    monkeypatch.setattr(migration, "install_generation_root", stop_after_root)
    with pytest.raises(OSError):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    unfinished = next(session.binding.store_path.rglob("staging")) / "unfinished"
    unfinished.mkdir()
    evidence = unfinished / "evidence"
    evidence.write_bytes(b"unresolved")

    def forbidden(*args, **kwargs):
        pytest.fail("Unrecognized staging reached installer")

    monkeypatch.setattr(migration, "install_generation_root", forbidden)
    with pytest.raises(ValueError, match="Unrecognized attachment evidence"):
        run(saved, current)
    assert evidence.read_bytes() == b"unresolved"
    assert inspect_migration(session.binding.store_path) == current
    assert load_migration_plan(session.binding.store_path)[0] == current


class StoppedProcess(BaseException):
    pass


@pytest.mark.parametrize("kind", ["partial", "empty", "complete", "manifest"])
def test_restart_accepts_only_exact_target_staging(saved, monkeypatch, kind):
    from nauro.store import generation_installation as roots
    from nauro.store._atomic import _tmp_name

    original = roots._stage_tree
    captured = []

    def interrupt(store, staging, projection):
        artifact = projection.artifacts[0]
        target = staging / "store" / artifact.path
        content = artifact.content
        if kind == "manifest":
            target, content = staging / "manifest.json", projection.manifest_json
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind != "complete":
            target = target.with_name(_tmp_name(target.name))
            content = content[: 0 if kind == "empty" else max(1, len(content) // 2)]
        target.write_bytes(content)
        captured.append((target, content))
        raise StoppedProcess()

    monkeypatch.setattr(roots, "_stage_tree", interrupt)
    with pytest.raises(StoppedProcess):
        run(saved)
    current = inspect_migration(saved[2].binding.store_path)
    assert current.phase == "installing"
    monkeypatch.setattr(roots, "_stage_tree", original)
    completed = run(saved, current)
    assert completed.phase == "completed"
    assert completed.migration_id == current.migration_id
    path, raw = captured[0]
    assert path.read_bytes() == raw


@pytest.mark.parametrize("kind", ["unknown", "prefix", "oversize", "symlink", "hardlink", "root"])
def test_unknown_staging_refuses_before_installer_sweep(saved, monkeypatch, kind):
    import os

    from nauro.store import generation_installation as roots
    from nauro.store._atomic import _tmp_name

    captured = []

    def interrupt(store, staging, projection):
        artifact = projection.artifacts[0]
        target = staging / "store" / artifact.path
        target.parent.mkdir(parents=True)
        partial = target.with_name(_tmp_name(target.name))
        partial.write_bytes(artifact.content[:1])
        if kind == "unknown":
            (staging / "unknown").write_bytes(b"preserve")
        elif kind == "prefix":
            partial.write_bytes(b"wrong")
        elif kind == "oversize":
            partial.write_bytes(artifact.content + b"extra")
        elif kind in {"symlink", "hardlink"}:
            other = store.parent / "outside"
            other.write_bytes(artifact.content)
            partial.unlink()
            if kind == "symlink":
                partial.symlink_to(other)
            else:
                os.link(other, partial)
        elif kind == "root":
            staging.rename(staging.with_name("unknown-root"))
        captured.append({str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()})
        raise StoppedProcess()

    monkeypatch.setattr(roots, "_stage_tree", interrupt)
    with pytest.raises(StoppedProcess):
        run(saved)
    current = inspect_migration(saved[2].binding.store_path)

    def forbidden(*args):
        pytest.fail("Invalid staging reached installer sweep")

    monkeypatch.setattr(roots, "_sweep_stale_staging", forbidden)
    with pytest.raises((ValueError, OSError)):
        run(saved, current)
    store = saved[2].binding.store_path
    assert {str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()} == captured[0]
    assert inspect_migration(store) == current


def test_installation_keeps_project_fence_while_source_is_vacant(saved, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from nauro.store.migration_admission import migration_write_guard

    record, _, session, *_ = saved
    source = session.binding.store_path
    entered, release = Event(), Event()
    rename = migration.os.rename

    def delayed(old, new):
        rename(old, new)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(migration.os, "rename", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, saved)
        try:
            assert entered.wait(10)
            assert not source.exists()
            assert migration.retained_source(record).is_dir()
            with (
                pytest.raises(MigrationAdmissionError, match="busy"),
                migration_write_guard(source, timeout=0),
            ):
                pytest.fail("A writer entered the vacant destination")
        finally:
            release.set()
        assert future.result(timeout=10).phase == "completed"


def test_installation_restart_preserves_nonempty_lock_evidence(saved, monkeypatch):
    from nauro.store.migration_admission import migration_lock_path

    _, _, session, *_ = saved
    install = migration._install
    monkeypatch.setattr(migration, "_install", lambda *a: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        run(saved)
    current = inspect_migration(session.binding.store_path)
    assert current.phase == "installing"
    lock = migration_lock_path(session.binding.store_path)
    lock.write_bytes(b"retained lock evidence")
    monkeypatch.setattr(migration, "_install", install)
    with pytest.raises(MigrationAdmissionError, match="lock contains"):
        run(saved, current)
    assert lock.read_bytes() == b"retained lock evidence"
    assert inspect_migration(session.binding.store_path) == current
    assert migration.retained_source(current).is_dir()
