"""Validated local binding and resumable cloud-store restoration."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock, Timeout
from nauro_core.doctor import diagnose_store
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from nauro.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR, PROJECT_MD
from nauro.store._atomic import atomic_write_bytes
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import bind_project_store_v2, get_store_path_v2
from nauro.store.repo_config import load_repo_config
from nauro.store.resolution import RepoResolution
from nauro.sync.cloud_projects import CloudProjectError, list_projects
from nauro.sync.etag import content_md5
from nauro.sync.remote import (
    PRESIGN_BATCH_LIMIT,
    PresignError,
    TransferSession,
    fetch_manifest,
    fetch_via_presigned_url,
    operation_session,
    request_presigned_urls,
)
from nauro.sync.state import FileState, SyncState, save_state
from nauro.sync.transfer import NullReporter, Reporter, download_with_retry

logger = logging.getLogger("nauro.store.recovery")

# Staging is a fixed per-project sibling of the destination, so a run that
# dies partway leaves work a later run can find and finish. The trailing
# hyphen in the legacy glob is what separates the random mkdtemp names an
# older restore stranded from the fixed name this one uses.
_STAGING_SUFFIX = ".restore"
_LEGACY_STAGING_GLOB_SUFFIX = _STAGING_SUFFIX + "-*"
_STAGING_LOCK_SUFFIX = _STAGING_SUFFIX + ".lock"
_STAGING_LEDGER_SUFFIX = _STAGING_SUFFIX + ".ledger.json"

# Two restores of one project would fight over the same staging tree. The wait
# is short: the second run has a human in front of it.
_RESTORE_LOCK_TIMEOUT = 5.0

# A batch's presigned URLs are re-minted this far before their ceiling, so a
# download never starts against a URL that expires while it runs.
_EXPIRY_MARGIN = timedelta(seconds=60)

_PROGRESS_INTERVAL = 25


class RecoveryError(RuntimeError):
    """A recovery action cannot complete without risking local state."""


class RestoreBusyError(RecoveryError):
    """Another process holds this project's restore staging directory."""


class EmptyCloudRecordError(RecoveryError):
    """The cloud project exists but has no stored record to restore.

    Distinct from :class:`RecoveryError` so attach can fall back to the
    empty-store-then-sync contract while a reconnect keeps treating an empty remote
    as a hard stop: a lost record must not read as a successful restore.
    """


class CloudManifestEntry(BaseModel):
    """Validated cloud manifest entry used during staged restoration."""

    model_config = ConfigDict(extra="ignore")

    path: StrictStr
    etag: StrictStr
    size: Annotated[StrictInt, Field(ge=0)] | None = None
    sha256: StrictStr | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, raw_path: str) -> str:
        if not raw_path or "\\" in raw_path:
            raise ValueError("manifest path must be a nonempty POSIX path")
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != raw_path:
            raise ValueError("manifest path must stay within the project store")
        return raw_path


class CloudPresignResult(BaseModel):
    """Validated download URL returned by the cloud presign endpoint."""

    model_config = ConfigDict(extra="ignore")

    verb: Literal["GET"]
    path: StrictStr
    url: StrictStr
    expires_at: datetime | None = None

    @field_validator("path", "url")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("presign fields must be nonempty")
        return value

    @field_validator("expires_at")
    @classmethod
    def as_utc(cls, value: datetime | None) -> datetime | None:
        """Read a zoneless ceiling as UTC — the server stamps UTC."""
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


_MANIFEST_ADAPTER = TypeAdapter(list[CloudManifestEntry])
_PRESIGN_ADAPTER = TypeAdapter(list[CloudPresignResult])


def _parse_manifest(raw_manifest: object) -> list[CloudManifestEntry]:
    try:
        return _MANIFEST_ADAPTER.validate_python(raw_manifest)
    except ValidationError as exc:
        raise RecoveryError("Invalid cloud manifest payload.") from exc


def _parse_presign_results(raw_results: object) -> list[CloudPresignResult]:
    try:
        return _PRESIGN_ADAPTER.validate_python(raw_results)
    except ValidationError as exc:
        raise RecoveryError("Invalid cloud restore presign payload.") from exc


def require_cloud_membership(project_id: str) -> str:
    """Return the cloud project name after verifying current membership."""
    try:
        projects = list_projects()
    except CloudProjectError as exc:
        raise RecoveryError(str(exc)) from exc
    match = next((project for project in projects if project["project_id"] == project_id), None)
    if match is None:
        raise RecoveryError(f"Project id {project_id!r} not found among your cloud projects.")
    name = match["name"]
    if not name:
        raise RecoveryError(f"Cloud project {project_id!r} has no valid name.")
    return name


def bind_local_store(repo_path: Path, store_path: Path) -> RepoResolution:
    """Bind an existing store using identity from the repository config."""
    config = load_repo_config(repo_path)
    bound = bind_project_store_v2(
        project_id=config["id"],
        name=config["name"],
        mode=config["mode"],
        repo_path=repo_path,
        store_path=store_path,
        server_url=config.get("server_url"),
    )
    return RepoResolution(bound, config["id"], config["name"])


def scaffold_empty_store(destination: Path) -> Path:
    """Ensure the store directory exists for a cloud project with no record yet.
    Only first-connection flows call this, and only after the cloud confirmed the remote
    record is empty: it mirrors remote state, never replaces a lost local record.
    """
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _validate_restored_store(store_path: Path) -> None:
    project_file = store_path / PROJECT_MD
    decisions_dir = store_path / DECISIONS_DIR
    if (
        not project_file.is_file()
        or project_file.is_symlink()
        or not decisions_dir.is_dir()
        or decisions_dir.is_symlink()
    ):
        raise RecoveryError("Cloud record is not a complete Nauro store.")
    diagnosis = diagnose_store(FilesystemStore(store_path))
    if not diagnosis.is_clean:
        raise RecoveryError("Cloud record failed decision-store integrity validation.")
    # A supersede backref orphan is repairable, not a reason to reject a
    # restore: the record is readable and every decision is intact. Name it
    # and the command that closes it rather than failing the recovery.
    if diagnosis.has_repairable_defects:
        logger.warning(
            "Restored record carries %d supersede backref orphan(s); run 'nauro repair'.",
            len(diagnosis.supersede_orphans),
        )


def _destination_is_available(destination: Path) -> bool:
    if not destination.exists():
        return True
    if not destination.is_dir():
        return False
    return next(destination.iterdir(), None) is None


class _IntegrityDefect(Enum):
    """Which manifest check a body failed. The value names it to the user."""

    SIZE = "size"
    CONTENT_HASH = "content-hash"
    HASH = "hash"


def _content_defect(content: bytes, entry: CloudManifestEntry) -> _IntegrityDefect | None:
    """Return the first check ``content`` fails for ``entry``, else None; a check applies only
    when its manifest field is present and usable (a multipart etag yields no md5 check); one
    judgment for both callers: a failing download stops the restore, a staged one is re-downloaded.
    """
    if entry.size is not None and len(content) != entry.size:
        return _IntegrityDefect.SIZE
    expected_md5 = content_md5(entry.etag)
    if (
        expected_md5 is not None
        and hashlib.md5(content, usedforsecurity=False).hexdigest() != expected_md5
    ):
        return _IntegrityDefect.CONTENT_HASH
    if entry.sha256 is not None and hashlib.sha256(content).hexdigest() != entry.sha256:
        return _IntegrityDefect.HASH
    return None


@dataclass
class _Progress:
    """Files in hand against the whole manifest, not against this run's share.

    A resumed run starts at the count it inherited from the staging directory,
    so ``k of N`` answers the question the user is actually asking: how much of
    the record is on disk.
    """

    total: int
    done: int = 0

    def record(self, reporter: Reporter) -> None:
        self.done += 1
        if self.done % _PROGRESS_INTERVAL == 0:
            reporter.info(f"{self.done}/{self.total} files")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _MintedBatch:
    """One presign response: a URL per path, plus the batch's TTL ceiling."""

    urls: dict[str, str]
    deadline: datetime | None


def _mint_download_urls(
    project_id: str, paths: Sequence[str], session: TransferSession
) -> _MintedBatch:
    """Mint one download URL per path, or refuse the batch."""
    operations = [{"verb": "GET", "path": path} for path in paths]
    try:
        results = _parse_presign_results(
            request_presigned_urls(project_id, operations, session=session)
        )
    except (AuthRefreshError, PresignError) as exc:
        raise RecoveryError(f"Cloud restore presign failed: {exc}") from exc
    urls: dict[str, str] = {}
    ceilings: list[datetime] = []
    for item in results:
        if item.path in urls:
            raise RecoveryError(f"Duplicate cloud restore URL for {item.path!r}.")
        urls[item.path] = item.url
        if item.expires_at is not None:
            ceilings.append(item.expires_at)
    if set(urls) != set(paths):
        raise RecoveryError("Cloud restore did not receive one download URL per manifest file.")
    return _MintedBatch(urls, min(ceilings) if ceilings else None)


class _ChunkUrls:
    """Presigned GET URLs for one download chunk, re-minted as the chunk drains.
    The server stamps one expiry ceiling per batch; when it is known, the chunk carries that
    single deadline and re-mints the outstanding tail rather than starting a download it cannot
    finish inside the margin. An undated batch cannot renew early; a stale-URL 403 re-mints
    through the same door either way.
    """

    def __init__(
        self,
        project_id: str,
        paths: Sequence[str],
        reporter: Reporter,
        session: TransferSession,
    ) -> None:
        self._project_id = project_id
        self._paths = list(paths)
        # Everything from the cursor on is still outstanding, so a re-mint
        # covers exactly the tail. in_order() owns the cursor and is the only
        # thing that hands a path out, which is what keeps that true.
        self._cursor = 0
        self._reporter = reporter
        self._session = session
        self._warned_undated = False
        self._mint()

    def _mint(self) -> None:
        self._batch = _mint_download_urls(
            self._project_id, self._paths[self._cursor :], self._session
        )
        if self._batch.deadline is None and not self._warned_undated:
            # Without a ceiling the restore cannot re-mint ahead of an expiry,
            # so it falls back to reacting to a refusal. Say so: a server that
            # stops stamping expiries would otherwise disable the proactive
            # path in silence.
            self._warned_undated = True
            self._reporter.warn(
                "The presign response carried no expiry, so this restore cannot renew "
                "a download URL before it lapses."
            )

    def url_for(self, path: str) -> str:
        return self._batch.urls[path]

    def remint(self) -> None:
        self._mint()

    def in_order(self) -> Iterator[str]:
        """Yield each path behind a URL that is not about to expire."""
        for index, path in enumerate(self._paths):
            self._cursor = index
            deadline = self._batch.deadline
            if deadline is not None and deadline - _utcnow() <= _EXPIRY_MARGIN:
                self.remint()
            yield path


def _staging_path(project_id: str, destination: Path) -> Path:
    return destination.parent / f".{project_id}{_STAGING_SUFFIX}"


@contextmanager
def _restore_lock(project_id: str, destination: Path) -> Iterator[None]:
    """Hold the project's restore lock, or refuse to start a second restore."""
    lock = FileLock(
        str(destination.parent / f".{project_id}{_STAGING_LOCK_SUFFIX}"),
        timeout=_RESTORE_LOCK_TIMEOUT,
    )
    try:
        lock.acquire()
    except Timeout as exc:
        raise RestoreBusyError(f"Another restore is in progress for project {project_id}.") from exc
    try:
        yield
    finally:
        lock.release()


def _discard(path: Path) -> None:
    """Remove ``path``, whatever kind of file occupies it.

    Best effort: every caller is cleaning up a failure or recreating the path.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _sweep_legacy_staging(project_id: str, parent: Path) -> None:
    """Remove staging directories a killed pre-resume run stranded.

    Those runs staged into a random ``mkdtemp`` name only this sweep can collect.
    """
    for stranded in parent.glob(f".{project_id}{_LEGACY_STAGING_GLOB_SUFFIX}"):
        _discard(stranded)


def _open_staging(staging: Path) -> None:
    """Make the staging path a directory, whatever is sitting on it.

    Only a directory a restore can resume from may occupy the path, so a file goes.
    """
    if staging.exists() and not (staging.is_dir() and not staging.is_symlink()):
        _discard(staging)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecoveryError(f"Could not open the restore staging area {staging}: {exc}") from exc


def _fetch_manifest_entries(
    project_id: str, session: TransferSession
) -> dict[str, CloudManifestEntry]:
    """Return the remote record's files, keyed by store-relative path."""
    try:
        rows = fetch_manifest(project_id, session=session)
    except (AuthRefreshError, PresignError) as exc:
        raise RecoveryError(f"Cloud manifest fetch failed: {exc}") from exc
    entries: dict[str, CloudManifestEntry] = {}
    for entry in _parse_manifest(rows):
        if entry.path in entries:
            raise RecoveryError(f"Duplicate cloud manifest path: {entry.path!r}.")
        entries[entry.path] = entry
    return entries


class StagedFile(BaseModel):
    """What one staged file held, recorded by the run that wrote it."""

    model_config = ConfigDict(extra="ignore")

    etag: StrictStr
    sha256: StrictStr


class StagingLedger(BaseModel):
    """The staged files a resume may trust, keyed by store-relative path."""

    model_config = ConfigDict(extra="ignore")

    files: dict[str, StagedFile] = Field(default_factory=dict)


def _prune_empty_directories(staging: Path) -> None:
    """Drop directories the audit emptied, so no phantom directory installs."""
    for path in sorted(staging.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink() and next(path.iterdir(), None) is None:
            path.rmdir()


@dataclass
class _StagingArea:
    """The staging directory plus the record of what this run put in it.

    The record holds each file's sha256 next to the ETag it was downloaded under, so
    a resume catches local damage and a rotated remote. It lives beside the staging
    directory, out of the tree the install renames and alive through a failed install.
    """

    path: Path
    ledger_path: Path
    ledger: StagingLedger

    def audit(self, entries: dict[str, CloudManifestEntry]) -> set[str]:
        """Return the staged paths a resume may keep, deleting every other one.

        An unrecorded file, a rotated remote, local damage, or a non-regular file goes.
        """
        kept: set[str] = set()
        try:
            for path in sorted(self.path.rglob("*")):
                if path.is_dir() and not path.is_symlink():
                    continue
                relative = path.relative_to(self.path).as_posix()
                entry = entries.get(relative)
                if entry is not None and self._still_good(path, relative, entry):
                    kept.add(relative)
                    continue
                path.unlink(missing_ok=True)
            _prune_empty_directories(self.path)
        except OSError as exc:
            raise RecoveryError(
                f"Could not audit the partial restore at {self.path}: {exc}"
            ) from exc
        self.ledger.files = {rel: rec for rel, rec in self.ledger.files.items() if rel in kept}
        return kept

    def _still_good(self, path: Path, relative: str, entry: CloudManifestEntry) -> bool:
        record = self.ledger.files.get(relative)
        if record is None or record.etag != entry.etag:
            return False
        if not path.is_file() or path.is_symlink():
            return False
        try:
            content = path.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(content).hexdigest() != record.sha256:
            return False
        return _content_defect(content, entry) is None

    def accept(self, relative: str, entry: CloudManifestEntry, content: bytes) -> None:
        """Land one downloaded file and record what it is.

        The record trails the write, so a kill costs a re-download, never false trust.
        """
        try:
            atomic_write_bytes(self.path / relative, content)
        except OSError as exc:
            raise RecoveryError(f"Could not stage cloud file {relative}: {exc}") from exc
        self.ledger.files[relative] = StagedFile(
            etag=entry.etag, sha256=hashlib.sha256(content).hexdigest()
        )
        self._save()

    def _save(self) -> None:
        try:
            atomic_write_bytes(self.ledger_path, self.ledger.model_dump_json().encode("utf-8"))
        except OSError as exc:
            raise RecoveryError(f"Could not record the partial restore: {exc}") from exc

    def discard(self) -> None:
        """Remove the staging tree and the record that described it."""
        _discard(self.path)
        _discard(self.ledger_path)


def _ledger_path(project_id: str, destination: Path) -> Path:
    return destination.parent / f".{project_id}{_STAGING_LEDGER_SUFFIX}"


def _discard_restore_state(project_id: str, destination: Path) -> None:
    """Remove any staging tree and record this project left beside ``destination``."""
    _discard(_staging_path(project_id, destination))
    _discard(_ledger_path(project_id, destination))


def _open_staging_area(project_id: str, destination: Path, reporter: Reporter) -> _StagingArea:
    """Open the staging directory and read whatever record accompanies it.

    An unreadable record is reported and treated as empty, costing a re-download.
    """
    staging = _staging_path(project_id, destination)
    ledger_path = _ledger_path(project_id, destination)
    _open_staging(staging)
    ledger = StagingLedger()
    if ledger_path.exists():
        try:
            ledger = StagingLedger.model_validate_json(ledger_path.read_bytes())
        except (OSError, ValidationError) as exc:
            reporter.warn(
                f"The record of the partial restore could not be read ({exc}); "
                "every staged file will be downloaded again."
            )
    return _StagingArea(staging, ledger_path, ledger)


def _human_bytes(total: int) -> str:
    if total >= 1_048_576:
        return f"{total / 1_048_576:.1f} MB"
    if total >= 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} bytes"


def _report_start(
    entries: dict[str, CloudManifestEntry], pending: Sequence[str], reporter: Reporter
) -> None:
    total_bytes = sum(entry.size or 0 for entry in entries.values())
    reporter.info(f"Restoring {len(entries)} files ({_human_bytes(total_bytes)}).")
    resumed = len(entries) - len(pending)
    if resumed:
        reporter.info(f"Resuming a partial restore: {resumed} of {len(entries)} files are staged.")


def _download_pending(
    project_id: str,
    area: _StagingArea,
    entries: dict[str, CloudManifestEntry],
    pending: Sequence[str],
    reporter: Reporter,
    progress: _Progress,
    session: TransferSession,
) -> None:
    """Download every outstanding file into staging, one presign chunk at a time."""
    for start in range(0, len(pending), PRESIGN_BATCH_LIMIT):
        chunk = _ChunkUrls(
            project_id,
            pending[start : start + PRESIGN_BATCH_LIMIT],
            reporter,
            session,
        )
        for relative in chunk.in_order():
            try:
                fetched = download_with_retry(
                    relative,
                    chunk,
                    lambda url: fetch_via_presigned_url(url, session=session),
                )
            except PresignError as exc:
                raise RecoveryError(f"Cloud download failed for {relative}: {exc}") from exc
            content = fetched.body
            entry = entries[relative]
            defect = _content_defect(content, entry)
            if defect is not None:
                raise RecoveryError(f"Cloud {defect.value} validation failed for {relative}.")
            area.accept(relative, entry, content)
            progress.record(reporter)


def _install_staged_store(staging: Path, destination: Path) -> None:
    """Move the verified tree onto the destination."""
    # os.replace cannot move a directory onto an existing directory on
    # Windows (even an empty one _destination_is_available admitted).
    # rmdir only succeeds on an empty directory, so this also re-enforces
    # the emptiness precondition at install time.
    if destination.exists():
        try:
            destination.rmdir()
        except OSError as exc:
            raise RecoveryError(
                f"Refusing to overwrite nonempty destination: {destination}."
            ) from exc
    try:
        os.replace(staging, destination)
    except OSError as exc:
        raise RecoveryError(
            f"Could not install the restored record at {destination}: {exc}"
        ) from exc


def _seed_sync_state(area: _StagingArea) -> None:
    """Record the assembled record as synced, inside the tree about to land.

    Written into staging, so it lands in the same rename as the files it describes.
    """
    now = _utcnow().isoformat()
    state = SyncState(
        files={
            relative: FileState(local_sha256=record.sha256, remote_etag=record.etag, last_sync=now)
            for relative, record in area.ledger.files.items()
        },
        # Only a complete, validated tree reaches this line, so the store holds
        # exactly what the manifest listed. That is the whole claim the stamp
        # makes, and it gates nothing: two status surfaces read it. Reporting
        # "never" for a store downloaded whole a moment ago would be the same
        # untruth as the absent entries above.
        last_full_sync=now,
    )
    try:
        save_state(area.path, state)
    except OSError as exc:
        raise RecoveryError(f"Could not record the restored record's sync state: {exc}") from exc


def _holds_complete_record(destination: Path) -> bool:
    """True when the destination already holds a complete, valid record."""
    try:
        _validate_restored_store(destination)
    except RecoveryError:
        return False
    return True


def _kept_message(staging: Path, progress: _Progress) -> str:
    return (
        f"Partial restore kept at {staging} "
        f"({progress.done} of {progress.total} files ready). "
        "Run the same command again to resume it, or delete that directory to start over."
    )


def restore_cloud_store(
    project_id: str, destination: Path, reporter: Reporter | None = None
) -> Path:
    """Restore a complete remote record into an absent or empty destination.

    Only a complete validated tree installs, in one rename; a failed run keeps staging.
    """
    surface: Reporter = reporter if reporter is not None else NullReporter()
    if not destination.is_absolute():
        raise RecoveryError(f"Restore destination must be absolute: {destination}.")
    # The canonical constraint is an external-binding rule: a mapped
    # store_path must be canonical so the registry never records a
    # symlink-relative location. The default home path is exempt — it is
    # derived, never recorded, and legitimately non-canonical wherever
    # NAURO_HOME or the user's home traverses a symlink (macOS $TMPDIR).
    if (
        destination != get_store_path_v2(project_id)
        and destination.resolve(strict=False) != destination
    ):
        raise RecoveryError(f"Restore destination must be canonical and absolute: {destination}.")
    if not _destination_is_available(destination):
        raise RecoveryError(f"Refusing to overwrite nonempty destination: {destination}.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with _restore_lock(project_id, destination):
        # The check above is stale by the time the lock is in hand: a run that
        # waited here may find the record already installed by the run it
        # waited for. That is the outcome it wanted, so it is not a failure.
        if not _destination_is_available(destination):
            _discard_restore_state(project_id, destination)
            if _holds_complete_record(destination):
                surface.info("Another restore completed this record first.")
                return destination
            raise RecoveryError(f"Refusing to overwrite nonempty destination: {destination}.")

        _sweep_legacy_staging(project_id, destination.parent)
        with operation_session() as session:
            entries = _fetch_manifest_entries(project_id, session)
            if not entries:
                # A record that is not there cannot complete a partial restore,
                # so nothing is kept to resume toward.
                _discard_restore_state(project_id, destination)
                raise EmptyCloudRecordError("Cloud project has no stored record to restore.")

            area = _open_staging_area(project_id, destination, surface)
            staged = area.audit(entries)
            pending = [relative for relative in entries if relative not in staged]
            _report_start(entries, pending, surface)

            progress = _Progress(total=len(entries), done=len(staged))
            try:
                _download_pending(
                    project_id,
                    area,
                    entries,
                    pending,
                    surface,
                    progress,
                    session,
                )
            except (RecoveryError, KeyboardInterrupt):
                surface.warn(_kept_message(area.path, progress))
                raise

        try:
            _validate_restored_store(area.path)
        except RecoveryError:
            # The assembled record is bad. Resuming would rebuild the same
            # badness, so the next run starts from nothing.
            area.discard()
            raise

        # Nothing enumerates the staged tree between the validation above and
        # the rename below, so a file added here is seen by nothing but the
        # install: _install_staged_store empties the destination and renames,
        # and that is all. A run that fails after this point and resumes writes
        # the state again from the ledger it rebuilds, over whatever it left.
        _seed_sync_state(area)

        # The content is verified from here. An install that fails now is a
        # local filesystem fault, not a bad record, so staging survives and the
        # next run installs it without downloading anything twice.
        try:
            _install_staged_store(area.path, destination)
        except RecoveryError:
            surface.warn(_kept_message(area.path, progress))
            raise
        _discard(area.ledger_path)
        surface.info(f"Restored {len(entries)} files.")
        return destination


__all__ = [
    "EmptyCloudRecordError",
    "RecoveryError",
    "RestoreBusyError",
    "bind_local_store",
    "require_cloud_membership",
    "restore_cloud_store",
    "scaffold_empty_store",
]
