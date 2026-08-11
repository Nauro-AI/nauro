"""Validated local binding and resumable cloud-store restoration."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import httpx
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

from nauro.cli.commands.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR, PROJECT_MD
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import bind_project_store_v2, get_store_path_v2
from nauro.store.repo_config import load_repo_config
from nauro.store.resolution import RepoResolution
from nauro.sync.cloud_projects import CloudProjectError, list_projects
from nauro.sync.remote import (
    PRESIGN_BATCH_LIMIT,
    PresignError,
    fetch_manifest,
    fetch_via_presigned_url,
    request_presigned_urls,
)
from nauro.sync.transfer import Reporter, download_with_retry

logger = logging.getLogger("nauro.store.recovery")

# Staging is a fixed per-project sibling of the destination, so a run that
# dies partway leaves work a later run can find and finish. The trailing
# hyphen in the legacy glob is what separates the random mkdtemp names an
# older restore stranded from the fixed name this one uses.
_STAGING_SUFFIX = ".restore"
_LEGACY_STAGING_GLOB_SUFFIX = _STAGING_SUFFIX + "-*"
_STAGING_LOCK_SUFFIX = _STAGING_SUFFIX + ".lock"

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

    Distinct from :class:`RecoveryError` so first-connection flows (attach)
    can fall back to the pre-existing empty-store-then-sync contract, while
    recovery flows (reconnect) keep treating an empty remote as a hard stop —
    a machine recovering a lost record must not mistake "nothing was ever
    pushed" for a successful restore.
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
    """Create the empty store directory for a cloud project with no record yet.

    Preserves attach's pre-recovery contract: the directory is created empty
    and files arrive via sync. Only first-connection flows call this, and only
    after the cloud has confirmed the remote record is empty — it is a mirror
    of the remote state, never a replacement for a lost local record.
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


def _etag_md5(raw_etag: str) -> str | None:
    """Return the ETag as a content MD5, or None when it cannot be one.

    An S3 ETag equals the object's MD5 only for single-part, non-KMS-encrypted
    uploads. Multipart ETags (``<md5>-<parts>``) and SSE-KMS/SSE-C ETags are
    opaque, so they yield None and the caller skips the MD5 comparison rather
    than failing a restore the sync pull path would have accepted. Size and
    optional sha256 checks still apply to every file.
    """
    value = raw_etag.strip('"').lower()
    if len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        return None
    return value


class _IntegrityDefect(Enum):
    """Which manifest check a body failed. The value names it to the user."""

    SIZE = "size"
    CONTENT_HASH = "content-hash"
    HASH = "hash"


def _content_defect(content: bytes, entry: CloudManifestEntry) -> _IntegrityDefect | None:
    """Return the check ``content`` fails for ``entry``, or None when it passes.

    One judgment serves both callers: a fresh download that fails it is a
    corrupt transfer and stops the restore, and a staged file that fails it is
    a leftover the resume re-downloads.
    """
    if entry.size is not None and len(content) != entry.size:
        return _IntegrityDefect.SIZE
    expected_md5 = _etag_md5(entry.etag)
    if (
        expected_md5 is not None
        and hashlib.md5(content, usedforsecurity=False).hexdigest() != expected_md5
    ):
        return _IntegrityDefect.CONTENT_HASH
    if entry.sha256 is not None and hashlib.sha256(content).hexdigest() != entry.sha256:
        return _IntegrityDefect.HASH
    return None


class _NullReporter:
    """Progress surface for callers that surface nothing (library use, hooks)."""

    def info(self, msg: str) -> None:
        """Discard routine progress."""

    def warn(self, msg: str) -> None:
        """Discard anomaly reports."""


@dataclass
class _Progress:
    """Files staged so far against the number this run set out to download."""

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


def _mint_download_urls(project_id: str, paths: Sequence[str]) -> _MintedBatch:
    """Mint one download URL per path, or refuse the batch."""
    operations = [{"verb": "GET", "path": path} for path in paths]
    try:
        results = _parse_presign_results(request_presigned_urls(project_id, operations))
    except (AuthRefreshError, PresignError, httpx.HTTPError) as exc:
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

    Minting immediately before a chunk downloads keeps a whole record off one
    TTL clock, which a slow network or a large record outlives. The server
    stamps one ceiling across a batch, so the chunk carries a single deadline
    and re-mints what is left rather than starting a download it cannot
    finish inside the margin. A stale-URL 403 re-mints through the same door.
    """

    def __init__(self, project_id: str, paths: Sequence[str]) -> None:
        self._project_id = project_id
        self._paths = list(paths)
        self._cursor = 0
        self._batch = _mint_download_urls(project_id, self._paths)

    def url_for(self, path: str) -> str:
        return self._batch.urls[path]

    def remint(self) -> None:
        self._batch = _mint_download_urls(self._project_id, self._paths[self._cursor :])

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


def _sweep_legacy_staging(project_id: str, parent: Path) -> None:
    """Remove staging directories a killed pre-resume run stranded.

    Those runs staged into a random ``mkdtemp`` name that no later run could
    find, so nothing but this sweep ever collects them.
    """
    for stranded in parent.glob(f".{project_id}{_LEGACY_STAGING_GLOB_SUFFIX}"):
        shutil.rmtree(stranded, ignore_errors=True)


def _fetch_manifest_entries(project_id: str) -> dict[str, CloudManifestEntry]:
    """Return the remote record's files, keyed by store-relative path."""
    try:
        rows = fetch_manifest(project_id)
    except (AuthRefreshError, PresignError, httpx.HTTPError) as exc:
        raise RecoveryError(f"Cloud manifest fetch failed: {exc}") from exc
    entries: dict[str, CloudManifestEntry] = {}
    for entry in _parse_manifest(rows):
        if entry.path in entries:
            raise RecoveryError(f"Duplicate cloud manifest path: {entry.path!r}.")
        entries[entry.path] = entry
    return entries


def _audit_staged_files(staging: Path, entries: dict[str, CloudManifestEntry]) -> set[str]:
    """Return the staged paths a resume may keep, deleting every other one.

    A staged file is kept only when the fresh manifest still lists it and it
    passes the checks a download must pass, so a resumed run can assemble
    nothing but the record the server holds now. Everything else — a
    mismatch, an unreadable file, a path the manifest dropped, anything that
    is not a regular file — goes, and the run downloads it again.
    """
    kept: set[str] = set()
    try:
        for path in sorted(staging.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(staging).as_posix()
            entry = entries.get(relative)
            if entry is not None and _staged_body_matches(path, entry):
                kept.add(relative)
                continue
            path.unlink(missing_ok=True)
        _prune_empty_directories(staging)
    except OSError as exc:
        raise RecoveryError(f"Could not audit the partial restore at {staging}: {exc}") from exc
    return kept


def _staged_body_matches(path: Path, entry: CloudManifestEntry) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return _content_defect(content, entry) is None


def _prune_empty_directories(staging: Path) -> None:
    """Drop directories the audit emptied, so no phantom directory installs."""
    for path in sorted(staging.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink() and next(path.iterdir(), None) is None:
            path.rmdir()


def _stage_file(staging: Path, relative: str, content: bytes) -> None:
    target = staging / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError as exc:
        raise RecoveryError(f"Could not stage cloud file {relative}: {exc}") from exc


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
    staging: Path,
    entries: dict[str, CloudManifestEntry],
    pending: Sequence[str],
    reporter: Reporter,
    progress: _Progress,
) -> None:
    """Download every outstanding file into staging, one presign chunk at a time."""
    for start in range(0, len(pending), PRESIGN_BATCH_LIMIT):
        chunk = _ChunkUrls(project_id, pending[start : start + PRESIGN_BATCH_LIMIT])
        for relative in chunk.in_order():
            try:
                content = download_with_retry(relative, chunk, fetch_via_presigned_url)
            except PresignError as exc:
                raise RecoveryError(f"Cloud download failed for {relative}: {exc}") from exc
            defect = _content_defect(content, entries[relative])
            if defect is not None:
                raise RecoveryError(f"Cloud {defect.value} validation failed for {relative}.")
            _stage_file(staging, relative, content)
            progress.record(reporter)


def _install_staged_store(staging: Path, destination: Path) -> None:
    """Validate the assembled tree and move it onto the destination."""
    _validate_restored_store(staging)
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
    os.replace(staging, destination)


def restore_cloud_store(
    project_id: str, destination: Path, reporter: Reporter | None = None
) -> Path:
    """Restore a complete remote record into an absent or empty destination.

    The restore resumes. It stages into a fixed per-project directory beside
    the destination and keeps that directory when a run fails partway, so the
    next run re-verifies what is there against a fresh manifest and downloads
    only the rest — a dropped connection halfway through a large record costs
    the remaining files, not all of them. Only a complete, validated tree
    installs, and the install stays one rename.
    """
    surface: Reporter = reporter if reporter is not None else _NullReporter()
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
    staging = _staging_path(project_id, destination)
    with _restore_lock(project_id, destination):
        _sweep_legacy_staging(project_id, destination.parent)
        entries = _fetch_manifest_entries(project_id)
        if not entries:
            # A record that is not there cannot complete a partial restore,
            # so nothing is kept to resume toward.
            shutil.rmtree(staging, ignore_errors=True)
            raise EmptyCloudRecordError("Cloud project has no stored record to restore.")

        staging.mkdir(parents=True, exist_ok=True)
        staged = _audit_staged_files(staging, entries)
        pending = [relative for relative in entries if relative not in staged]
        _report_start(entries, pending, surface)

        progress = _Progress(total=len(pending))
        try:
            _download_pending(project_id, staging, entries, pending, surface, progress)
        except (RecoveryError, KeyboardInterrupt):
            surface.warn(
                f"Partial restore kept ({progress.done} of {progress.total} files downloaded). "
                "Run the same command again to resume it."
            )
            raise

        # Past this point staging is either installed or discarded: a tree
        # that fails validation is a bad record, and rebuilding the same
        # badness on the next run helps nobody.
        installed = False
        try:
            _install_staged_store(staging, destination)
            installed = True
        finally:
            if not installed:
                shutil.rmtree(staging, ignore_errors=True)
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
