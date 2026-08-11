"""Restore behavior over an unreliable network: retry, re-mint, and resume."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from filelock import FileLock

from nauro.store import recovery
from nauro.store.recovery import RecoveryError, RestoreBusyError, restore_cloud_store
from nauro.sync import transfer
from nauro.sync.remote import PresignTransferError
from tests.test_recovery import PID, _decision_bytes, _store_files


def _http_fault(status: int) -> PresignTransferError:
    return PresignTransferError("Presigned GET", status=status, detail="refused")


def _transport_fault() -> PresignTransferError:
    return PresignTransferError("Presigned GET", transport=True, detail="connection reset")


class _Cloud:
    """A scriptable remote record behind the restore's three network calls.

    Every minted URL carries its mint generation, so a test can prove a
    download ran against re-minted URLs rather than stale ones. Presign calls,
    fetches, and backoff waits are all recorded.
    """

    # The deployed presign endpoint stamps a 900-second ceiling on every URL,
    # so that is the default here; ``ttl=None`` models a server that stopped.
    def __init__(
        self, files: dict[str, bytes], ttl: timedelta | None = timedelta(seconds=900)
    ) -> None:
        self.files = files
        self.ttl = ttl
        self.generation = 0
        self.mints: list[list[str]] = []
        self.fetched: list[str] = []
        self.faults: dict[str, list[Exception]] = {}
        self.always_fail: dict[str, Exception] = {}
        self.waits: list[float] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(recovery, "fetch_manifest", self.manifest)
        monkeypatch.setattr(recovery, "request_presigned_urls", self.presign)
        monkeypatch.setattr(recovery, "fetch_via_presigned_url", self.fetch)
        monkeypatch.setattr(transfer, "pause", self.waits.append)

    def manifest(self, _project_id: str) -> list[dict]:
        return [
            {
                "path": path,
                "etag": f'"{hashlib.md5(content).hexdigest()}"',
                "size": len(content),
            }
            for path, content in sorted(self.files.items())
        ]

    def presign(self, _project_id: str, operations: list[dict[str, str]]) -> list[dict]:
        self.generation += 1
        self.mints.append([operation["path"] for operation in operations])
        row: dict[str, str] = {}
        if self.ttl is not None:
            expires = datetime.now(timezone.utc) + self.ttl
            row["expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        return [
            {
                "verb": "GET",
                "path": operation["path"],
                "url": f"memory://{self.generation}/{operation['path']}",
                **row,
            }
            for operation in operations
        ]

    def fetch(self, url: str) -> bytes:
        _generation, _, path = url.removeprefix("memory://").partition("/")
        self.fetched.append(path)
        queued = self.faults.get(path)
        if queued:
            raise queued.pop(0)
        standing = self.always_fail.get(path)
        if standing is not None:
            raise standing
        return self.files[path]


class _RecordingReporter:
    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.warn_lines: list[str] = []

    def info(self, msg: str) -> None:
        self.info_lines.append(msg)

    def warn(self, msg: str) -> None:
        self.warn_lines.append(msg)


def _staging(destination) -> object:
    return destination.parent / f".{PID}.restore"


def test_transient_transport_fault_retries_with_backoff_and_restores(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.faults["project.md"] = [_transport_fault(), _transport_fault()]
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert (destination / "project.md").read_bytes() == files["project.md"]
    assert cloud.fetched.count("project.md") == 3
    # Full jitter: each wait falls inside the doubling ceiling, never above it.
    assert [wait <= ceiling for wait, ceiling in zip(cloud.waits, [0.5, 1.0])] == [True, True]
    assert len(cloud.waits) == 2


def test_permanent_http_fault_aborts_on_the_first_attempt(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.always_fail["project.md"] = _http_fault(404)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="download failed"):
        restore_cloud_store(PID, destination)

    assert cloud.fetched.count("project.md") == 1
    assert cloud.waits == []
    assert not destination.exists()


def test_stale_url_remints_the_rest_of_the_chunk_and_succeeds(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.faults["project.md"] = [_http_fault(403)]
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    result = restore_cloud_store(PID, destination)

    assert result == destination
    # The re-mint covers the files still outstanding, not the whole record.
    assert cloud.mints[1] == ["project.md", "stack.md", "state_current.md"]
    assert cloud.waits == []


def test_second_refusal_after_a_fresh_mint_is_permanent(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.always_fail["project.md"] = _http_fault(403)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="download failed"):
        restore_cloud_store(PID, destination)

    assert cloud.fetched.count("project.md") == 2
    assert len(cloud.mints) == 2


def test_aborted_restore_keeps_staging_and_a_rerun_fetches_only_the_rest(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.always_fail["stack.md"] = _http_fault(404)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()

    with pytest.raises(RecoveryError):
        restore_cloud_store(PID, destination, reporter)

    staging = _staging(destination)
    assert sorted(
        path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()
    ) == ["decisions/001-initial-setup.md", "open-questions.md", "project.md"]
    # The count is against the whole record, and it names the kept directory.
    assert reporter.warn_lines == [
        f"Partial restore kept at {staging} (3 of {len(files)} files ready). "
        "Run the same command again to resume it, or delete that directory to start over."
    ]

    cloud.always_fail.clear()
    cloud.fetched.clear()
    cloud.mints.clear()
    resumed = _RecordingReporter()

    result = restore_cloud_store(PID, destination, resumed)

    assert result == destination
    assert cloud.fetched == ["stack.md", "state_current.md"]
    assert cloud.mints == [["stack.md", "state_current.md"]]
    assert not staging.exists()
    assert f"Resuming a partial restore: 3 of {len(files)} files are staged." in resumed.info_lines


@pytest.mark.parametrize("damage", ["shorter", "same-size"])
def test_resume_redownloads_a_corrupt_staged_file(damage, tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    staging = _staging(destination)
    for relative, content in files.items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    corrupt = b"x" if damage == "shorter" else b"x" * len(files["stack.md"])
    (staging / "stack.md").write_bytes(corrupt)
    (staging / "dropped.md").write_bytes(b"absent from the manifest")

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert cloud.fetched == ["stack.md"]
    assert (destination / "stack.md").read_bytes() == files["stack.md"]
    assert not (destination / "dropped.md").exists()


def test_a_deadline_inside_the_margin_remints_before_each_download(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files, ttl=timedelta(seconds=30))
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    result = restore_cloud_store(PID, destination)

    assert result == destination
    # One mint opens the chunk; every file then starts inside the margin.
    assert len(cloud.mints) == 1 + len(files)
    assert cloud.mints[-1] == ["state_current.md"]


def test_a_deadline_beyond_the_margin_mints_once(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files, ttl=timedelta(seconds=900))
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    assert restore_cloud_store(PID, destination) == destination
    assert len(cloud.mints) == 1


def test_a_batch_without_an_expiry_warns_once(tmp_path, monkeypatch):
    """A server that stops stamping expiries must not disable the renewal quietly."""
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files, ttl=None)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()

    assert restore_cloud_store(PID, destination, reporter) == destination
    assert [line for line in reporter.warn_lines if "no expiry" in line] == [
        "The presign response carried no expiry, so this restore cannot renew "
        "a download URL before it lapses."
    ]
    assert len(cloud.mints) == 1


def test_progress_reports_start_cadence_and_completion(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    for number in range(100, 130):
        files[f"decisions/{number}-extra.md"] = _decision_bytes(number)
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()

    restore_cloud_store(PID, destination, reporter)

    assert reporter.info_lines[0].startswith(f"Restoring {len(files)} files (")
    assert f"25/{len(files)} files" in reporter.info_lines
    assert reporter.info_lines[-1] == f"Restored {len(files)} files."
    assert reporter.warn_lines == []


def test_staging_writes_go_through_the_atomic_primitive(tmp_path, monkeypatch):
    """Every staged file lands by tmp-then-rename, never by a truncating write.

    A manifest entry with no size and an opaque ETag gives the resume audit
    nothing to check, so a short file would pass the audit and install as
    content. The primitive removes the case rather than narrowing it.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    written: list[str] = []
    real_write = recovery.atomic_write_bytes

    def counted(path, data):
        written.append(path.name)
        real_write(path, data)

    monkeypatch.setattr(recovery, "atomic_write_bytes", counted)

    restore_cloud_store(PID, destination)

    assert recovery.atomic_write_bytes is counted
    assert len(written) == len(files)
    assert (destination / "project.md").read_bytes() == files["project.md"]


def test_a_kill_mid_restore_keeps_only_whole_files(tmp_path, monkeypatch):
    """An interrupt keeps the staging directory, and every survivor is complete."""
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.always_fail["project.md"] = KeyboardInterrupt()
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()

    with pytest.raises(KeyboardInterrupt):
        restore_cloud_store(PID, destination, reporter)

    staging = _staging(destination)
    survivors = {
        path.relative_to(staging).as_posix(): path.read_bytes()
        for path in staging.rglob("*")
        if path.is_file()
    }
    assert survivors == {relative: files[relative] for relative in survivors}
    assert survivors
    assert any(str(staging) in line for line in reporter.warn_lines)


def test_resume_removes_a_tmp_sibling_a_kill_stranded(tmp_path, monkeypatch):
    """The audit deletes every staged path the manifest does not list.

    A tmp sibling from an interrupted atomic write is one of those paths, so
    the resume clears it instead of installing it into the store.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    staging = _staging(destination)
    staging.mkdir(parents=True)
    (staging / ".project.md.0123456789abcdef.tmp").write_bytes(b"half a file")

    restore_cloud_store(PID, destination)

    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == sorted(files)


def test_a_file_squatting_on_the_staging_path_is_cleared(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    destination.parent.mkdir(parents=True)
    _staging(destination).write_bytes(b"not a staging directory")

    assert restore_cloud_store(PID, destination) == destination
    assert (destination / "project.md").read_bytes() == files["project.md"]


def test_legacy_staging_directories_are_swept(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    stranded = destination.parent / f".{PID}.restore-ab12cd"
    stranded.mkdir(parents=True)
    (stranded / "project.md").write_bytes(b"orphaned by a killed run")
    # A file wearing the same pattern is litter under the same reading.
    (destination.parent / f".{PID}.restore-ef34gh").write_bytes(b"orphan")

    restore_cloud_store(PID, destination)

    assert not list(destination.parent.glob(f".{PID}.restore-*"))


def test_a_second_restore_refuses_while_one_is_running(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    monkeypatch.setattr(recovery, "_RESTORE_LOCK_TIMEOUT", 0.05)
    destination = tmp_path / "projects" / PID
    destination.parent.mkdir(parents=True)
    held = FileLock(str(destination.parent / f".{PID}.restore.lock"))
    held.acquire()

    try:
        with pytest.raises(RestoreBusyError, match="in progress"):
            restore_cloud_store(PID, destination)
    finally:
        held.release()

    assert cloud.fetched == []
