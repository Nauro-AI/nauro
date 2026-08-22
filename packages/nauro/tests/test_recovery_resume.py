"""Restore behavior over an unreliable network: retry, re-mint, and resume."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from filelock import FileLock

from nauro.store import recovery
from nauro.store.recovery import RecoveryError, RestoreBusyError, restore_cloud_store
from nauro.sync import transfer
from nauro.sync.remote import (
    FetchedObject,
    RetryClassification,
    TransferBoundaryError,
    TransferOperation,
    TransportFaultKind,
    WriteOutcome,
    classify_status,
)
from nauro.sync.state import SYNC_STATE_FILE
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_recovery import PID, _decision_bytes, _store_files


def _http_fault(status: int) -> TransferBoundaryError:
    classification = classify_status(status, operation=TransferOperation.GET)
    return TransferBoundaryError(
        operation=TransferOperation.GET,
        origin="invalid-origin",
        status=status,
        kind=classification.kind,
        retry=classification.retry,
        write_outcome=classification.write_outcome,
    )


def _transport_fault() -> TransferBoundaryError:
    return TransferBoundaryError(
        operation=TransferOperation.GET,
        origin="invalid-origin",
        status=None,
        kind=TransportFaultKind.CONNECT_ERROR,
        retry=RetryClassification.TRANSIENT,
        write_outcome=WriteOutcome.NOT_APPLICABLE,
    )


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
        # Paths served with a multipart ETag and no size, the shape that gives
        # the manifest nothing to check a staged body against.
        self.opaque: set[str] = set()
        self.etag_revision = 0

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(recovery, "fetch_manifest", self.manifest)
        monkeypatch.setattr(recovery, "request_presigned_urls", self.presign)
        monkeypatch.setattr(recovery, "fetch_via_presigned_url", self.fetch)
        monkeypatch.setattr(transfer, "pause", self.waits.append)

    def manifest(self, _project_id: str, **_kwargs) -> list[dict]:
        rows = []
        for path, content in sorted(self.files.items()):
            if path in self.opaque:
                rows.append(
                    {"path": path, "etag": f'"{"0" * 31}{self.etag_revision}-2"'},
                )
                continue
            rows.append(
                {
                    "path": path,
                    "etag": f'"{hashlib.md5(content).hexdigest()}"',
                    "size": len(content),
                }
            )
        return rows

    def presign(self, _project_id: str, operations: list[dict[str, str]], **_kwargs) -> list[dict]:
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

    def fetch(self, url: str, **_kwargs) -> FetchedObject:
        _generation, _, path = url.removeprefix("memory://").partition("/")
        self.fetched.append(path)
        queued = self.faults.get(path)
        if queued:
            raise queued.pop(0)
        standing = self.always_fail.get(path)
        if standing is not None:
            raise standing
        return FetchedObject(self.files[path], f'"{hashlib.md5(self.files[path]).hexdigest()}"')


class _RecordingReporter:
    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.warn_lines: list[str] = []

    def info(self, msg: str) -> None:
        self.info_lines.append(msg)

    def warn(self, msg: str) -> None:
        self.warn_lines.append(msg)


def _staging(destination: Path) -> Path:
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


def test_a_transient_fault_gets_four_attempts_and_no_more(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.always_fail["project.md"] = _transport_fault()
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="download failed"):
        restore_cloud_store(PID, destination)

    assert cloud.fetched.count("project.md") == 4
    assert len(cloud.waits) == 3


def test_a_remint_does_not_spend_the_transient_budget(tmp_path, monkeypatch):
    """A re-mint refreshes a credential; it is not a failed network attempt.

    The URL that replaces a stale one has to be able to ride out a dropped
    connection too, so the fresh URL starts with the whole budget.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.faults["project.md"] = [
        _http_fault(403),
        _transport_fault(),
        _transport_fault(),
        _transport_fault(),
    ]
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    result = restore_cloud_store(PID, destination)

    assert result == destination
    # One refusal, then a full budget of three retries and the success.
    assert cloud.fetched.count("project.md") == 5
    assert len(cloud.mints) == 2
    assert len(cloud.waits) == 3


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


def _abort_after_staging_all_but_last(cloud: _Cloud, destination: Path) -> Path:
    """Leave a genuine partial restore behind: four files staged, one to go."""
    cloud.always_fail["state_current.md"] = _http_fault(404)
    with pytest.raises(RecoveryError):
        restore_cloud_store(PID, destination)
    cloud.always_fail.clear()
    cloud.fetched.clear()
    return _staging(destination)


@pytest.mark.parametrize("damage", ["shorter", "same-size"])
def test_resume_redownloads_a_corrupt_staged_file(damage, tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    staging = _abort_after_staging_all_but_last(cloud, destination)
    corrupt = b"x" if damage == "shorter" else b"x" * len(files["stack.md"])
    (staging / "stack.md").write_bytes(corrupt)
    (staging / "dropped.md").write_bytes(b"absent from the manifest")

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert cloud.fetched == ["stack.md", "state_current.md"]
    assert (destination / "stack.md").read_bytes() == files["stack.md"]
    assert not (destination / "dropped.md").exists()


def test_resume_redownloads_a_same_size_corruption_behind_an_opaque_etag(tmp_path, monkeypatch):
    """The manifest alone cannot catch this one.

    A multipart ETag is not a content MD5 and the server sends no size for it,
    so nothing in the manifest disagrees with a same-length corruption. The
    digest this run recorded when it wrote the file is what catches it.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.opaque.add("stack.md")
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    staging = _abort_after_staging_all_but_last(cloud, destination)
    (staging / "stack.md").write_bytes(b"x" * len(files["stack.md"]))

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert cloud.fetched == ["stack.md", "state_current.md"]
    assert (destination / "stack.md").read_bytes() == files["stack.md"]


def test_resume_redownloads_a_file_the_remote_rotated(tmp_path, monkeypatch):
    """A staged file that is intact locally but stale remotely is refetched.

    The ETag recorded at download time is compared against the fresh manifest,
    so a remote file that moved on is re-downloaded even when the staged bytes
    are exactly what this run wrote.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.opaque.add("stack.md")
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    _abort_after_staging_all_but_last(cloud, destination)
    # Same length, new content, new opaque ETag: only the recorded ETag differs.
    cloud.files["stack.md"] = b"y" * len(files["stack.md"])
    cloud.etag_revision += 1

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert cloud.fetched == ["stack.md", "state_current.md"]
    assert (destination / "stack.md").read_bytes() == cloud.files["stack.md"]


def test_the_restore_record_never_reaches_the_store(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    ledger = destination.parent / f".{PID}.restore.ledger.json"

    _abort_after_staging_all_but_last(cloud, destination)

    # It exists while a partial restore is resumable, and it is not staged.
    assert ledger.is_file()
    assert not (_staging(destination) / ledger.name).exists()

    restore_cloud_store(PID, destination)

    assert not ledger.exists()
    assert [path.name for path in destination.rglob("*") if "ledger" in path.name] == []


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
        if "ledger" not in path.name:
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
    ) == sorted([*files, SYNC_STATE_FILE])


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


def test_a_failed_install_keeps_the_verified_tree_for_a_resume(tmp_path, monkeypatch):
    """A filesystem fault at install time is not a reason to download again.

    The content passed every check by then. The next run installs what is
    already staged and fetches nothing.
    """
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()

    staging = _staging(destination)
    real_replace = recovery.os.replace

    def refuse_the_install_rename(source, target):
        # Only the install moves the staging directory itself; the atomic
        # staging writes rename tmp siblings and must keep working.
        if Path(source) == staging:
            raise OSError("cross-device link")
        return real_replace(source, target)

    monkeypatch.setattr(recovery.os, "replace", refuse_the_install_rename)

    with pytest.raises(RecoveryError, match="install"):
        restore_cloud_store(PID, destination, reporter)

    assert staging.is_dir()
    assert any("Partial restore kept" in line for line in reporter.warn_lines)
    assert cloud.fetched == sorted(files)

    monkeypatch.undo()
    cloud.install(monkeypatch)
    cloud.fetched.clear()

    result = restore_cloud_store(PID, destination)

    assert result == destination
    assert cloud.fetched == []
    assert (destination / "project.md").read_bytes() == files["project.md"]
    assert not staging.exists()


def test_a_store_that_fails_validation_discards_the_staging_tree(tmp_path, monkeypatch):
    """A bad assembled record must not resume into the same bad record."""
    files = _store_files(tmp_path / "source")
    decision = next(path for path in files if path.startswith("decisions/"))
    files[decision] = files[decision].replace(b"superseded_by: null", b"superseded_by: '999'")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    with pytest.raises(RecoveryError, match="integrity"):
        restore_cloud_store(PID, destination)

    assert not _staging(destination).exists()
    assert not (destination.parent / f".{PID}.restore.ledger.json").exists()


def _lock_that_lets_another_run_finish(populate) -> object:
    """A restore lock whose waiter finds the destination already taken."""
    real_lock = recovery._restore_lock

    @contextmanager
    def racing_lock(project_id, destination):
        with real_lock(project_id, destination):
            populate(destination)
            yield

    return racing_lock


def test_a_waiter_accepts_the_record_the_winner_installed(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID
    reporter = _RecordingReporter()
    monkeypatch.setattr(
        recovery,
        "_restore_lock",
        _lock_that_lets_another_run_finish(lambda path: scaffold_project_store("nauro", path)),
    )

    result = restore_cloud_store(PID, destination, reporter)

    assert result == destination
    assert cloud.fetched == []
    assert any("Another restore completed" in line for line in reporter.info_lines)


def test_a_waiter_refuses_a_destination_that_is_not_a_record(tmp_path, monkeypatch):
    files = _store_files(tmp_path / "source")
    cloud = _Cloud(files)
    cloud.install(monkeypatch)
    destination = tmp_path / "projects" / PID

    def litter(path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "unrelated.txt").write_bytes(b"not a record")

    monkeypatch.setattr(recovery, "_restore_lock", _lock_that_lets_another_run_finish(litter))

    with pytest.raises(RecoveryError, match="nonempty destination"):
        restore_cloud_store(PID, destination)

    assert cloud.fetched == []


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
