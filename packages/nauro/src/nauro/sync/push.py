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
from pathlib import Path

import typer
from nauro_core.constants import MAX_BRIEF_BYTES

from nauro.cli.commands.auth import load_access_token
from nauro.store.registry import is_cloud_project

logger = logging.getLogger("nauro.sync")


def push_store_to_cloud(project_id: str, store_path: Path) -> bool:
    """Push store changes after a local sync.

    Cloud-mode projects with a token → presign push. Cloud-mode without
    a token → warn and return False (caller surfaces exit 1). Anything
    else (local-mode or unknown id) → True since nothing was expected to
    upload.

    A presign/auth-refresh failure during the push is reported and maps
    to False; per-file PUT failures are logged and skipped.
    """
    from nauro.cli.commands.auth import AuthRefreshError
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


def push_changed_files(project_id: str, store_path: Path) -> int:
    """Upload every store file whose local sha differs from sync-state.

    POSTs ``/sync/presign`` for the changed paths, then PUTs each one to
    its presigned URL, recording the returned ETag in sync-state. Returns
    the number of files successfully pushed.

    Raises :class:`~nauro.cli.commands.auth.AuthRefreshError` or
    :class:`~nauro.sync.remote.PresignError` if minting the presigned
    URLs fails; per-file PUT failures are logged and skipped so a partial
    upload still records the files that did land.
    """
    from nauro.sync.merge import should_skip
    from nauro.sync.remote import (
        PresignError,
        put_via_presigned_url,
        request_presigned_urls,
    )
    from nauro.sync.state import compute_sha256, load_state, save_state, update_file_state

    state = load_state(store_path)

    changed: list[tuple[str, str, Path]] = []
    for local_file in store_path.rglob("*"):
        if not local_file.is_file():
            continue
        try:
            rel = str(local_file.relative_to(store_path))
        except ValueError:
            continue
        if should_skip(rel) or rel.startswith(".conflict-backup") or rel.startswith("__pycache__"):
            continue

        # Shared briefs (context/*.md) are written via the agent's filesystem
        # tool + sync, bypassing the MCP write-tool size caps, so the push path
        # is the only place MAX_BRIEF_BYTES can be enforced. An over-cap brief is
        # skipped loudly and left on disk (never silently dropped, never recorded
        # in sync-state) so it keeps warning until trimmed, and so it cannot make
        # the agent believe it shared context that never propagated. The skip is
        # per-file: other store files still sync. Scoped to ``.md`` because the
        # skill writes briefs only as ``<slug>.md``; non-``.md`` content under
        # ``context/`` falls back to the (uncapped) general store behavior, same
        # as every other store file — it is not a new storage-bomb surface.
        if rel.startswith("context/") and rel.endswith(".md"):
            size = local_file.stat().st_size
            if size > MAX_BRIEF_BYTES:
                typer.echo(
                    f"  Warning: brief {rel} is {size:,} bytes, over the "
                    f"{MAX_BRIEF_BYTES:,}-byte limit, and was not pushed. "
                    "It stays on your machine; trim it under the cap and "
                    "re-sync to share it.",
                    err=True,
                )
                continue

        local_sha = compute_sha256(local_file)
        fs = state.files.get(rel)
        if fs is None or fs.local_sha256 != local_sha:
            changed.append((rel, local_sha, local_file))

    if not changed:
        save_state(store_path, state)
        return 0

    operations = [{"verb": "PUT", "path": rel} for rel, _sha, _path in changed]
    urls = request_presigned_urls(project_id, operations)

    if len(urls) < len(operations):
        logger.warning("Presign returned %d URLs for %d ops", len(urls), len(operations))

    url_by_path = {
        entry["path"]: entry["url"]
        for entry in urls
        if isinstance(entry, dict) and entry.get("verb") == "PUT"
    }

    pushed = 0
    for rel, local_sha, local_file in changed:
        url = url_by_path.get(rel)
        if not url:
            continue
        try:
            new_etag = put_via_presigned_url(url, local_file)
            if new_etag:
                update_file_state(state, rel, local_sha, new_etag)
                pushed += 1
        except PresignError:
            logger.exception("Failed to push %s", rel)

    save_state(store_path, state)
    return pushed


__all__ = ["push_changed_files", "push_store_to_cloud"]
