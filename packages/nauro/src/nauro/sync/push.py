"""Store to cloud push over the legacy presign transport.

Shared by ``nauro sync`` (push half of pull-then-push) and ``nauro link
--cloud`` (the first push after promoting a local project). Both callers
scan the store for files whose local sha differs from sync-state, mint
presigned PUT URLs, and upload directly to S3.

The push is token-gated and offline-tolerant: a project that is not
cloud-mode, or a cloud-mode project without a token, never reaches the
network here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import typer
from nauro_core.constants import MAX_BRIEF_BYTES

from nauro.cli.commands.auth import AuthRefreshError, load_access_token
from nauro.store.registry import is_cloud_project
from nauro.sync.lock import CLI_SYNC_LOCK_TIMEOUT, SyncLockTimeoutError, sync_lock
from nauro.sync.merge import CONFLICT_BACKUP_DIR, should_skip
from nauro.sync.remote import (
    PresignError,
    RetryClassification,
    TransferBoundaryError,
    TransferSession,
    WriteOutcome,
    operation_session,
)
from nauro.sync.state import SyncState, compute_sha256
from nauro.sync.transfer import backoff_delay, pause

logger = logging.getLogger("nauro.sync")

_PUT_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class PushCandidate:
    relative_path: str
    local_sha256: str
    local_file: Path


@dataclass(frozen=True)
class OversizedBrief:
    relative_path: str
    size: int


@dataclass(frozen=True)
class PushPlan:
    candidates: tuple[PushCandidate, ...]
    oversized_briefs: tuple[OversizedBrief, ...]


@dataclass(frozen=True)
class PushReport:
    """One truthful partition of the paths selected for upload."""

    planned: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    missing_urls: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()
    origin_aborted: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.verified == self.planned and not (
            self.missing_urls or self.failed or self.ambiguous or self.origin_aborted
        )

    @property
    def pushed(self) -> int:
        return len(self.verified)

    @property
    def warning(self) -> str:
        parts = []
        for label, values in (
            ("verified", self.verified),
            ("missing URL", self.missing_urls),
            ("failed", self.failed),
            ("pending observation", self.ambiguous),
            ("origin aborted", self.origin_aborted),
        ):
            if values:
                parts.append(f"{len(values)} {label}")
        return f"cloud push incomplete ({', '.join(parts)})"


def plan_push(store_path: Path, state: SyncState) -> PushPlan:
    """Select existing local files that the legacy PUT path can send."""
    candidates: list[PushCandidate] = []
    oversized_briefs: list[OversizedBrief] = []
    for local_file in store_path.rglob("*"):
        if not local_file.is_file():
            continue
        try:
            rel = str(local_file.relative_to(store_path))
        except ValueError:
            continue
        if should_skip(rel) or rel.startswith(CONFLICT_BACKUP_DIR) or rel.startswith("__pycache__"):
            continue

        # Filesystem-authored briefs bypass the MCP write size limit, so sync
        # applies the same limit before they enter the upload plan.
        if rel.startswith("context/") and rel.endswith(".md"):
            size = local_file.stat().st_size
            if size > MAX_BRIEF_BYTES:
                oversized_briefs.append(OversizedBrief(rel, size))
                continue
        local_sha = compute_sha256(local_file)
        file_state = state.files.get(rel)
        if file_state is None or file_state.local_sha256 != local_sha:
            candidates.append(PushCandidate(rel, local_sha, local_file))
    return PushPlan(tuple(candidates), tuple(oversized_briefs))


def push_store_to_cloud(
    project_id: str,
    store_path: Path,
    *,
    session: TransferSession | None = None,
) -> PushReport:
    """Push store changes and render failures for an explicit sync."""
    if not is_cloud_project(project_id):
        return PushReport()
    if not load_access_token():
        typer.echo(
            "  Warning: this is a cloud-mode project but you're not authenticated.\n"
            "  Your local snapshot is captured, but nothing was uploaded.\n"
            "  Run 'nauro auth login' to configure credentials.",
            err=True,
        )
        return PushReport(failed=("authentication",))
    try:
        report = push_changed_files(project_id, store_path, session=session)
    except SyncLockTimeoutError as exc:
        typer.echo(f"  Warning: {exc}", err=True)
        return PushReport(failed=("sync lock",))
    except AuthRefreshError as exc:
        typer.echo(f"  {exc}", err=True)
        return PushReport(failed=("authentication",))
    except PresignError as exc:
        logger.warning("Cloud push boundary failed: %s", exc)
        typer.echo(f"  Warning: cloud push failed ({exc})", err=True)
        return PushReport(failed=("transport",))
    if report.verified:
        typer.echo(f"  Pushed {len(report.verified)} file(s) to S3")
    if not report.is_complete:
        typer.echo(f"  Warning: {report.warning}", err=True)
    return report


def push_changed_files(
    project_id: str,
    store_path: Path,
    *,
    lock_timeout: float = CLI_SYNC_LOCK_TIMEOUT,
    session: TransferSession | None = None,
) -> PushReport:
    """Upload changed files and checkpoint each verified result."""
    with operation_session(session) as active:
        with sync_lock(store_path, lock_timeout):
            return _push_changed_files_locked(project_id, store_path, active)


def _push_changed_files_locked(
    project_id: str, store_path: Path, session: TransferSession
) -> PushReport:
    from nauro.sync.remote import request_presigned_urls, urls_by_path
    from nauro.sync.state import load_state, save_state

    state = load_state(store_path)
    plan = plan_push(store_path, state)
    for brief in plan.oversized_briefs:
        typer.echo(
            f"  Warning: brief {brief.relative_path} is {brief.size:,} bytes, over the "
            f"{MAX_BRIEF_BYTES:,}-byte limit, and was not pushed. "
            "It stays on your machine; trim it under the cap and re-sync to share it.",
            err=True,
        )
    planned = tuple(candidate.relative_path for candidate in plan.candidates)
    if not plan.candidates:
        save_state(store_path, state)
        return PushReport(planned=planned)

    operations = [{"verb": "PUT", "path": path} for path in planned]
    try:
        urls = request_presigned_urls(project_id, operations, session=session)
    except PresignError as exc:
        aborted = planned if _origin_failure(exc) else ()
        failed = () if aborted else planned
        return PushReport(planned=planned, failed=failed, origin_aborted=aborted)

    if len(urls) < len(operations):
        logger.warning("Presign returned %d URLs for %d ops", len(urls), len(operations))
    url_by_path, skipped = urls_by_path(urls, "PUT")
    for entry in skipped:
        logger.warning("Skipping %s", entry)

    verified: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    ambiguous: list[str] = []
    origin_aborted: list[str] = []

    for index, candidate in enumerate(plan.candidates):
        path = candidate.relative_path
        url = url_by_path.get(path)
        if not url:
            missing.append(path)
            continue
        try:
            payload = candidate.local_file.read_bytes()
        except OSError:
            failed.append(path)
            continue
        payload_sha = hashlib.sha256(payload).hexdigest()
        try:
            result = _put_frozen(project_id, path, url, payload, session)
        except TransferBoundaryError as exc:
            if exc.aborts_origin:
                origin_aborted.append(path)
                continue
            if exc.write_outcome is WriteOutcome.AMBIGUOUS:
                outcome = _reconcile(project_id, path, payload_sha, session)
                if outcome.kind is _ReconcileKind.VERIFIED:
                    if not _checkpoint(store_path, state, path, payload_sha, outcome.etag):
                        failed.extend(planned[index:])
                        break
                    verified.append(path)
                elif outcome.kind is _ReconcileKind.DIVERGENT:
                    failed.append(path)
                else:
                    ambiguous.append(path)
                continue
            failed.append(path)
            continue
        except AuthRefreshError:
            failed.append(path)
            continue

        if result.outcome is WriteOutcome.WRITTEN_UNVERIFIED:
            outcome = _reconcile(project_id, path, payload_sha, session)
            if outcome.kind is _ReconcileKind.DIVERGENT:
                failed.append(path)
                continue
            if outcome.kind is not _ReconcileKind.VERIFIED:
                ambiguous.append(path)
                continue
            etag = outcome.etag
        else:
            etag = result.etag
        if not _checkpoint(store_path, state, path, payload_sha, etag):
            failed.extend(planned[index:])
            break
        verified.append(path)

    return PushReport(
        planned=planned,
        verified=tuple(verified),
        missing_urls=tuple(missing),
        failed=tuple(failed),
        ambiguous=tuple(ambiguous),
        origin_aborted=tuple(origin_aborted),
    )


def _origin_failure(error: PresignError) -> bool:
    return isinstance(error, TransferBoundaryError) and error.aborts_origin


def _put_frozen(
    project_id: str,
    path: str,
    initial_url: str,
    payload: bytes,
    session: TransferSession,
):
    from nauro.sync.remote import put_via_presigned_url, request_presigned_urls, urls_by_path

    url = initial_url
    failures = 0
    reminted = False
    while True:
        try:
            return put_via_presigned_url(url, payload, session=session)
        except TransferBoundaryError as exc:
            failures += 1
            if (
                exc.retry is RetryClassification.EXPIRED_CANDIDATE
                and exc.write_outcome is WriteOutcome.NOT_WRITTEN
                and not reminted
            ):
                reminted = True
                failures = 0
                rows = request_presigned_urls(
                    project_id,
                    [{"verb": "PUT", "path": path}],
                    session=session,
                )
                urls, _skipped = urls_by_path(rows, "PUT")
                replacement = urls.get(path)
                if replacement is None:
                    raise exc
                url = replacement
                continue
            if (
                exc.retry is not RetryClassification.TRANSIENT
                or exc.write_outcome is not WriteOutcome.NOT_WRITTEN
                or failures >= _PUT_MAX_ATTEMPTS
            ):
                raise
            pause(backoff_delay(failures))


class _ReconcileKind(Enum):
    VERIFIED = "verified"
    DIVERGENT = "divergent"
    PENDING = "pending"


@dataclass(frozen=True)
class _ReconcileOutcome:
    kind: _ReconcileKind
    etag: str = ""


def _reconcile(
    project_id: str, path: str, payload_sha: str, session: TransferSession
) -> _ReconcileOutcome:
    from nauro.sync.remote import (
        fetch_manifest,
        fetch_via_presigned_url,
        request_presigned_urls,
        urls_by_path,
    )

    try:
        manifest = fetch_manifest(project_id, session=session)
        if not any(isinstance(row, dict) and row.get("path") == path for row in manifest):
            return _ReconcileOutcome(_ReconcileKind.PENDING)
        rows = request_presigned_urls(
            project_id,
            [{"verb": "GET", "path": path}],
            session=session,
        )
        urls, _skipped = urls_by_path(rows, "GET")
        url = urls.get(path)
        if url is None:
            return _ReconcileOutcome(_ReconcileKind.PENDING)
        fetched = fetch_via_presigned_url(url, session=session)
    except (AuthRefreshError, PresignError):
        return _ReconcileOutcome(_ReconcileKind.PENDING)
    if not fetched.etag:
        return _ReconcileOutcome(_ReconcileKind.PENDING)
    if hashlib.sha256(fetched.body).hexdigest() != payload_sha:
        return _ReconcileOutcome(_ReconcileKind.DIVERGENT)
    return _ReconcileOutcome(_ReconcileKind.VERIFIED, fetched.etag)


def _checkpoint(
    store_path: Path,
    state: SyncState,
    path: str,
    payload_sha: str,
    etag: str,
) -> bool:
    from nauro.sync.state import save_state, update_file_state

    update_file_state(state, path, payload_sha, etag)
    try:
        save_state(store_path, state)
    except OSError:
        logger.exception("Failed to checkpoint cloud push state for %s", path)
        return False
    return True


__all__ = [
    "OversizedBrief",
    "PushCandidate",
    "PushPlan",
    "PushReport",
    "plan_push",
    "push_changed_files",
    "push_store_to_cloud",
]
