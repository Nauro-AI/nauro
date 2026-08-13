"""Store → cloud push over the presign transport.

Shared by ``nauro sync`` (push half of pull-then-push) and ``nauro link
--cloud`` (the first push after promoting a local project). Both callers
scan the store for files whose local sha differs from sync-state, mint
presigned PUT URLs, and upload directly to S3.

The push is token-gated and offline-tolerant: a project that is not
cloud-mode, or a cloud-mode project without a token, never reaches the
network here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import typer
from nauro_core.constants import MAX_BRIEF_BYTES

from nauro.cli.commands.auth import load_access_token
from nauro.store.registry import is_cloud_project
from nauro.sync.lock import CLI_SYNC_LOCK_TIMEOUT, sync_lock
from nauro.sync.merge import CONFLICT_BACKUP_DIR, should_skip
from nauro.sync.state import SyncState, compute_sha256

logger = logging.getLogger("nauro.sync")


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
        fs = state.files.get(rel)
        if fs is None or fs.local_sha256 != local_sha:
            candidates.append(PushCandidate(rel, local_sha, local_file))

    return PushPlan(tuple(candidates), tuple(oversized_briefs))


def push_store_to_cloud(project_id: str, store_path: Path) -> bool:
    """Push store changes after a local sync.

    Cloud-mode projects with a token → presign push. Cloud-mode without
    a token → warn and return False (caller surfaces exit 1). Anything
    else (local-mode or unknown id) → True since nothing was expected to
    upload.

    A presign/auth-refresh failure during the push is reported and maps
    to False; per-file PUT failures are logged and skipped. A concurrent sync
    holding the store lock past the bounded wait is also a reported failure:
    an explicit sync must not report success for an upload it never attempted.
    """
    from nauro.cli.commands.auth import AuthRefreshError
    from nauro.sync.lock import SyncLockTimeoutError
    from nauro.sync.remote import PresignError

    if not is_cloud_project(project_id):
        return True
    if not load_access_token():
        typer.echo(
            "  Warning: this is a cloud-mode project but you're not authenticated.\n"
            "  Your local snapshot is captured, but nothing was uploaded.\n"
            "  Run 'nauro auth login' to configure credentials.",
            err=True,
        )
        return False

    try:
        pushed = push_changed_files(project_id, store_path)
    except SyncLockTimeoutError as exc:
        typer.echo(f"  Warning: {exc}", err=True)
        return False
    except AuthRefreshError as exc:
        typer.echo(f"  {exc}", err=True)
        return False
    except PresignError as exc:
        logger.exception("Failed to request presigned PUT URLs")
        typer.echo(f"  Warning: presign request failed ({exc})", err=True)
        return False

    if pushed:
        typer.echo(f"  Pushed {pushed} file(s) to S3")
    return True


def push_changed_files(
    project_id: str,
    store_path: Path,
    *,
    lock_timeout: float = CLI_SYNC_LOCK_TIMEOUT,
) -> int:
    """Upload every store file whose local sha differs from sync-state.

    POSTs ``/sync/presign`` for the changed paths, then PUTs each one to
    its presigned URL, recording the returned ETag in sync-state. Returns
    the number of files successfully pushed.

    Held under the store's sync lock for the whole run: a pull resolving a
    decision-number collision renames local files, and a push scanning the
    store mid-sequence would upload a half-applied rename.

    Raises :class:`~nauro.cli.commands.auth.AuthRefreshError` or
    :class:`~nauro.sync.remote.PresignError` if minting the presigned
    URLs fails; per-file PUT failures are logged and skipped so a partial
    upload still records the files that did land.
    Raises :class:`~nauro.sync.lock.SyncLockTimeoutError` when another sync holds
    the store lock for longer than ``lock_timeout``.
    """
    with sync_lock(store_path, lock_timeout):
        return _push_changed_files_locked(project_id, store_path)


def _push_changed_files_locked(project_id: str, store_path: Path) -> int:
    from nauro.sync.remote import (
        PresignError,
        put_via_presigned_url,
        request_presigned_urls,
        urls_by_path,
    )
    from nauro.sync.state import load_state, save_state, update_file_state

    state = load_state(store_path)
    plan = plan_push(store_path, state)
    for brief in plan.oversized_briefs:
        typer.echo(
            f"  Warning: brief {brief.relative_path} is {brief.size:,} bytes, over the "
            f"{MAX_BRIEF_BYTES:,}-byte limit, and was not pushed. "
            "It stays on your machine; trim it under the cap and re-sync to share it.",
            err=True,
        )

    if not plan.candidates:
        save_state(store_path, state)
        return 0

    operations = [{"verb": "PUT", "path": candidate.relative_path} for candidate in plan.candidates]
    urls = request_presigned_urls(project_id, operations)

    if len(urls) < len(operations):
        logger.warning("Presign returned %d URLs for %d ops", len(urls), len(operations))

    url_by_path, skipped = urls_by_path(urls, "PUT")
    for entry in skipped:
        logger.warning("Skipping unreadable presign entry %s", entry)

    pushed = 0
    for candidate in plan.candidates:
        url = url_by_path.get(candidate.relative_path)
        if not url:
            continue
        try:
            new_etag = put_via_presigned_url(url, candidate.local_file)
            if new_etag:
                update_file_state(
                    state,
                    candidate.relative_path,
                    candidate.local_sha256,
                    new_etag,
                )
                pushed += 1
        except PresignError:
            logger.exception("Failed to push %s", candidate.relative_path)

    save_state(store_path, state)
    return pushed


__all__ = [
    "OversizedBrief",
    "PushCandidate",
    "PushPlan",
    "plan_push",
    "push_changed_files",
    "push_store_to_cloud",
]
