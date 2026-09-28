from __future__ import annotations

import base64
import hashlib
import os
import shutil
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
from nauro.store.config import save_config
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmissionError,
    inspect_migration,
    migration_lock_path,
    migration_write_guard,
)
from nauro.store.registry import (
    bind_project_store_v2,
    load_registry_v2,
    remove_project_v2,
    save_registry_v2,
)
from nauro.store.replica_control import ReplicaControlBusyError
from nauro.store.resolution import StoreResolutionError
from nauro.sync import migration_preservation as preservation
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.generation_connection import attachment_connection
from nauro.sync.generation_credentials import AccountRecord
from nauro.sync.migration_admission import (
    decide_migration_assessment,
    load_migration_plan,
    save_migration_assessment,
)
from nauro.sync.remote import TransferBoundaryError
from tests.test_generation_migration_assessment import _symlink
from tests.test_generation_migration_plan import _assessment
from tests.windows_refusal import REFUSALS, chmod_calls, refusing


@pytest.fixture
def saved(tmp_path, monkeypatch):
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("External network"))
    for key in (
        "NAURO_AUTH0_DOMAIN",
        "NAURO_AUTH0_CLIENT_ID",
        "NAURO_API_URL",
        "NAURO_AUTH0_AUDIENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    save_config(
        {
            "auth0_domain": "issuer.example",
            "auth0_client_id": "client",
            "api_url": "https://mcp.nauro.ai",
            "auth0_audience": "https://mcp.nauro.ai/mcp",
        }
    )
    binding, assessment = _assessment(tmp_path)
    (binding.store_path / "empty").mkdir()
    from nauro.store.generation_migration_assessment import assess_legacy_migration

    assessment = assess_legacy_migration(assessment.projection)
    plan = prepare_legacy_migration_plan(assessment)
    record = decide_migration_assessment(save_migration_assessment(plan), preserve=True)
    connection = attachment_connection(DEFAULT_AUTH_REDIRECT_URI)
    credentials = connection.store()
    with credentials.locked():
        credentials.write(
            AccountRecord(
                revision="a" * 64,
                binding=connection.binding(),
                state="active",
                user_id=record.actor,
                subject="owner",
                access_token="test-token",
                refresh_token="test-refresh",
                expires_at=int(time.time()) + 600,
            )
        )
    repo = tmp_path / "repo"
    repo.mkdir()
    bind_project_store_v2(
        project_id=record.project_id,
        name="Synthetic",
        mode="cloud",
        repo_path=repo,
        store_path=binding.store_path,
        server_url=record.endpoint,
    )
    control = {
        "role": "owner",
        "status": 200,
        "manifest": assessment.projection.manifest_json,
        "identity": assessment.projection.target.identity.model_dump(),
    }
    calls = []

    def wire(request):
        calls.append(request.url.path)
        if request.url.host == "objects.example":
            artifacts = {a.path: a.content for a in assessment.projection.artifacts}
            return httpx.Response(200, content=artifacts[request.url.path.lstrip("/")])
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == "/projects":
            return httpx.Response(
                200,
                json={
                    "authority": "generation_owner",
                    "projects": [{"project_id": record.project_id, "role": control["role"]}],
                },
            )
        if request.url.path == "/generations/presign":
            return httpx.Response(
                200,
                json={
                    "projection": control["identity"],
                    "urls": [
                        {"path": a.path, "url": "https://objects.example/" + a.path}
                        for a in assessment.projection.artifacts
                    ],
                    "expires_at": "2999-12-31T23:59:59Z",
                },
            )
        assert request.url.path == "/generations/projection"
        return httpx.Response(
            control["status"],
            json={
                "projection": control["identity"],
                "manifest_base64": base64.b64encode(control["manifest"]).decode(),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        session = InitialAttachmentSession(binding, repo, connection, client)
        yield record, plan, session, control, credentials, calls


def test_preservation_keeps_source_and_exact_saved_plan_on_restart(saved):
    record, plan, session, _, _, calls = saved
    source = session.binding.store_path
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    root = preservation.preserve_migration_source(record, session)
    assert root == plan.backup_root
    for entry in plan.entries:
        assert (root / entry.destination_path).read_bytes() == before[Path(entry.source_path)]
    assert (root / "legacy/empty").is_dir()
    assert (root / "plan.json").read_bytes() == plan.manifest_json
    assert {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    } == before
    assert inspect_migration(source) == record
    loaded, raw = load_migration_plan(source)
    assert raw == plan.manifest_json
    fresh = InitialAttachmentSession(
        session.binding, session.repo, session.connection, session.client
    )
    assert preservation.preserve_migration_source(loaded, fresh) == root
    assert calls == ["/projects", "/generations/projection"] * 4
    with pytest.raises(MigrationAdmissionError, match="incomplete"), migration_write_guard(source):
        pytest.fail("Preservation reopened writes")


@pytest.mark.parametrize(
    "failure", ["copy", "file_barrier", "directory_barrier", "final_authorization"]
)
def test_interruption_retains_identity_and_requires_explicit_continuation(
    saved, monkeypatch, failure
):
    record, plan, session, control, _, calls = saved
    copies = preservation._copy
    sync_file = preservation.sync_file
    sync_parents = preservation.sync_parents

    def interrupted_copy(source, root, entry):
        if failure == "copy":
            (root / preservation._pending(entry)).write_bytes(b"partial")
            raise OSError("disk full")
        copies(source, root, entry)
        if failure == "final_authorization":
            control["role"] = "viewer"

    def file_barrier(paths, path):
        if path.name != "plan.json":
            raise OSError("file barrier")
        sync_file(paths, path)

    def directory_barrier(paths, path):
        if any((plan.backup_root / e.destination_path).exists() for e in plan.entries):
            raise OSError("directory barrier")
        sync_parents(paths, path)

    monkeypatch.setattr(preservation, "_copy", interrupted_copy)
    if failure == "file_barrier":
        monkeypatch.setattr(preservation, "sync_file", file_barrier)
    if failure == "directory_barrier":
        monkeypatch.setattr(preservation, "sync_parents", directory_barrier)
    with pytest.raises((OSError, ValueError, TransferBoundaryError, StoreResolutionError)):
        preservation.preserve_migration_source(record, session)
    count = len(calls)
    assert load_migration_plan(session.binding.store_path) == (record, plan.manifest_json)
    assert len(calls) == count
    assert session.binding.store_path.is_dir()
    monkeypatch.setattr(preservation, "_copy", copies)
    monkeypatch.setattr(preservation, "sync_file", sync_file)
    monkeypatch.setattr(preservation, "sync_parents", sync_parents)
    control["role"] = "owner"
    assert preservation.preserve_migration_source(record, session) == plan.backup_root


@pytest.mark.parametrize("damage", ["source", "backup", "extra", "link", "hardlink", "plan"])
def test_changed_evidence_refuses_unchanged(saved, damage):
    record, plan, session, _, _, _ = saved
    root = preservation.preserve_migration_source(record, session)
    path = root / plan.entries[0].destination_path
    if damage == "source":
        (session.binding.store_path / "state_current.md").write_text("changed")
    elif damage == "backup":
        path.write_bytes(b"changed")
    elif damage == "extra":
        (root / "unknown").write_bytes(b"retained")
    elif damage == "plan":
        (root / "plan.json").write_bytes(b"{}")
    elif damage == "link":
        path.unlink()
        _symlink(path, session.binding.store_path / plan.entries[0].source_path)
    else:
        (root / "alias").hardlink_to(path)
    with pytest.raises((OSError, ValueError, TransferBoundaryError, StoreResolutionError)):
        preservation.preserve_migration_source(record, session)
    assert inspect_migration(session.binding.store_path) == record
    assert root.exists()
    assert session.binding.store_path.exists()


@pytest.mark.parametrize(
    "change", ["role", "revoked", "scope", "expired", "revision", "registry", "endpoint"]
)
def test_changed_access_refuses_before_backup(saved, change):
    record, plan, session, control, credentials, _ = saved
    if change == "role":
        control["role"] = "viewer"
    elif change == "revoked":
        control["status"] = 403
    elif change == "scope":
        control["identity"]["projection_scope_id"] = "b" * 64
    elif change in {"expired", "revision"}:
        with credentials.locked():
            original = credentials.read()
            credentials.write(
                original.model_copy(
                    update={"expires_at": 1} if change == "expired" else {"revision": "b" * 64}
                )
            )
    elif change == "registry":
        assert remove_project_v2(record.project_id) is True
    else:
        save_config({"api_url": "https://other.example"})
    with pytest.raises((OSError, ValueError, TransferBoundaryError, StoreResolutionError)):
        preservation.preserve_migration_source(record, session)
    assert not plan.backup_root.exists()
    assert inspect_migration(session.binding.store_path) == record


def test_competing_continuation_cannot_enter_copy(saved, monkeypatch):
    record, plan, session, _, _, _ = saved
    entered, release = Event(), Event()
    original = preservation._copy

    def delayed(*args):
        entered.set()
        assert release.wait(10)
        original(*args)

    monkeypatch.setattr(preservation, "_copy", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(preservation.preserve_migration_source, record, session)
        try:
            assert entered.wait(10)
            with pytest.raises(ReplicaControlBusyError, match="busy"):
                preservation.preserve_migration_source(record, session)
        finally:
            release.set()
        assert future.result(timeout=10) == plan.backup_root


def test_source_change_during_copy_never_reports_completion(saved, monkeypatch):
    record, plan, session, _, _, _ = saved
    original = preservation._copy

    def changed(source, root, entry):
        original(source, root, entry)
        if entry.source_path == plan.entries[-1].source_path:
            (source / "state_current.md").write_text("Changed after copying")

    monkeypatch.setattr(preservation, "_copy", changed)
    with pytest.raises(MigrationAdmissionError, match="fresh assessment"):
        preservation.preserve_migration_source(record, session)
    assert inspect_migration(session.binding.store_path) == record
    assert plan.backup_root.is_dir()


def test_unknown_backup_control_link_is_not_skipped(saved):
    record, plan, session, _, _, _ = saved
    root = preservation.preserve_migration_source(record, session)
    link = root / ".replica-control.lock"
    _symlink(link, root / "missing")
    with pytest.raises(ValueError):
        preservation.preserve_migration_source(record, session)
    assert link.is_symlink()


@pytest.mark.parametrize("change", ["store", "endpoint", "removed"])
def test_fresh_session_cannot_adopt_changed_registration(saved, tmp_path, change):
    record, plan, session, _, _, calls = saved
    registry = load_registry_v2()
    if change == "store":
        other = tmp_path / "other" / record.project_id
        shutil.copytree(session.binding.store_path, other)
        registry["projects"][record.project_id]["store_path"] = str(other)
    elif change == "endpoint":
        registry["projects"][record.project_id]["server_url"] = "https://other.example"
    else:
        del registry["projects"][record.project_id]
    save_registry_v2(registry)
    fresh = InitialAttachmentSession(
        session.binding, session.repo, session.connection, session.client
    )
    with pytest.raises((MigrationAdmissionError, ValueError, StoreResolutionError)):
        preservation.preserve_migration_source(record, fresh)
    assert not plan.backup_root.exists()
    assert calls == []
    assert load_migration_plan(session.binding.store_path) == (record, plan.manifest_json)


def test_preservation_refuses_lock_evidence_without_truncating(saved):
    record, plan, session, _, _, calls = saved
    lock = migration_lock_path(session.binding.store_path)
    lock.write_bytes(b"retained evidence")
    with pytest.raises(MigrationAdmissionError, match="lock contains"):
        preservation.preserve_migration_source(record, session)
    assert lock.read_bytes() == b"retained evidence"
    assert calls == []
    assert not plan.backup_root.exists()


def _binary_entry(tmp_path, raw: bytes):
    source, root = tmp_path / "source", tmp_path / "backup"
    (source / "decisions").mkdir(parents=True)
    (source / "decisions/001-crlf.md").write_bytes(raw)
    for directory in (".pending", "legacy/decisions"):
        (root / directory).mkdir(parents=True)
    entry = preservation._Entry(
        source_path="decisions/001-crlf.md",
        destination_path="legacy/decisions/001-crlf.md",
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        source_class="identical",
        disposition="legacy_backup",
        recovery_kind="archive_only",
        recovery_routes=[],
        offer_export=False,
    )
    return source, root, entry


def test_preserved_bytes_round_trip_in_binary_mode(tmp_path, monkeypatch):
    raw = b"# Decision\r\nline\n\x1a after the DOS end-of-file byte\r\n"
    source, root, entry = _binary_entry(tmp_path, raw)
    native = getattr(os, "O_BINARY", 0)
    binary, opened, real_open = native or 0x40000000, [], os.open
    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)

    def spy(path, flags, *args):
        if sys._getframe(1).f_globals["__name__"] == preservation.__name__:
            opened.append((Path(path).name, bool(flags & binary)))
        return real_open(path, flags if native else flags & ~binary, *args)

    monkeypatch.setattr(os, "open", spy)
    preservation._copy(source, root, entry)
    preservation._publish_plan(root, raw)
    assert (root / entry.destination_path).read_bytes() == raw
    assert (root / "plan.json").read_bytes() == raw
    assert opened == [
        (preservation._pending(entry).removeprefix(".pending/"), True),
        ("001-crlf.md", True),
        (preservation._plan_pending(raw), True),
    ]


@pytest.mark.parametrize(("windows", "winerror", "reason"), REFUSALS)
def test_unreadable_legacy_source_stops_copy_with_typed_refusal(
    tmp_path, monkeypatch, windows, winerror, reason
):
    source, root, entry = _binary_entry(tmp_path, b"retained\n")
    src, calls, real_open = source / entry.source_path, [], os.open
    refuse = refusing(monkeypatch, windows, winerror, calls)
    monkeypatch.setattr(
        os, "open", lambda path, *a: refuse(path) if Path(path) == src else real_open(path, *a)
    )
    chmods = chmod_calls(monkeypatch)
    with pytest.raises(MigrationAdmissionError) as raised:
        preservation._copy(source, root, entry)
    assert str(raised.value) == f"Cannot copy {src}: {reason}."
    assert (calls, chmods) == ([src], [])
    assert src.read_bytes() == b"retained\n"
    assert not (root / entry.destination_path).exists()


def test_windows_open_without_error_code_names_both_reasons(tmp_path, monkeypatch):
    source, root, entry = _binary_entry(tmp_path, b"retained\n")
    src, calls, real_open = source / entry.source_path, [], os.open
    refuse = refusing(monkeypatch, True, None, calls)
    monkeypatch.setattr(
        os, "open", lambda path, *a: refuse(path) if Path(path) == src else real_open(path, *a)
    )
    with pytest.raises(MigrationAdmissionError, match="read-only or another process has it open"):
        preservation._copy(source, root, entry)
