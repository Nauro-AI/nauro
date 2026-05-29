"""nauro sync — Capture a snapshot and regenerate AGENTS.md in associated repos."""

import logging
from pathlib import Path

import typer

from nauro.cli.commands.auth import load_access_token
from nauro.cli.utils import resolve_target_project
from nauro.store.registry import (
    RegistrySchemaError,
    get_project_v2,
    is_cloud_project,
    load_registry,
)
from nauro.store.snapshot import capture_snapshot
from nauro.store.validator import print_warnings, validate_store
from nauro.templates.agents_md import regenerate_agents_md_for_project

logger = logging.getLogger("nauro.sync")


def _registry_repo_paths(project_key: str) -> list[str]:
    """Return repo paths for ``project_key`` from v2 (preferred) or v1 registry."""
    try:
        v2_entry = get_project_v2(project_key)
    except RegistrySchemaError:
        v2_entry = None
    if v2_entry is not None:
        return list(v2_entry.get("repo_paths", []))
    registry = load_registry()
    return list(registry["projects"].get(project_key, {}).get("repo_paths", []))


def sync(
    message: str = typer.Option("", "--message", "-m", help="Sync message stored in the snapshot."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    status: bool = typer.Option(False, "--status", help="Show sync status."),
) -> None:
    """Capture a snapshot and regenerate AGENTS.md in each associated repo.

    With cloud sync configured, pulls from the server first (git-style
    pull-then-push), then pushes the updated store back. Project state in
    state_current.md is not touched — use the MCP ``update_state`` tool to
    record what changed.
    """
    if status:
        _show_status(project)
        return

    project_name, store_path = resolve_target_project(project)
    # store_path.name is the project_id under v2 (id-keyed) or name under v1.
    project_key = store_path.name
    trigger = message or "manual sync"

    _pull_from_cloud(project_key, store_path)

    version = capture_snapshot(store_path, trigger=trigger)

    for repo_str in _registry_repo_paths(project_key):
        if not Path(repo_str).is_dir():
            typer.echo(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_str}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json",
                err=True,
            )

    updated_repos = regenerate_agents_md_for_project(project_key, store_path)

    pushed = _push_to_cloud(project_key, store_path)

    if pushed:
        if is_cloud_project(project_key):
            typer.echo(f"Synced {project_name} — snapshot v{version:03d}")
        else:
            typer.echo(
                f"Captured snapshot v{version:03d} for {project_name}"
                " (local-only project; nothing to upload)."
            )
        for repo_path in updated_repos:
            typer.echo(f"  Updated AGENTS.md: {repo_path}")
    else:
        typer.echo(
            f"Snapshot v{version:03d} captured locally; not uploaded.",
            err=True,
        )
        raise typer.Exit(code=1)

    warnings = validate_store(store_path)
    if warnings:
        print_warnings(warnings)


def _pull_from_cloud(project_id: str, store_path: Path) -> int:
    """Pull remote changes via the manifest + presign endpoints.

    No-op when the project is not v2 cloud-mode (v1 entries and v2
    local-mode have no presign target) or when no Auth0 token is
    configured.
    """
    if not is_cloud_project(project_id):
        return 0
    if not load_access_token():
        return 0
    return _pull_via_presign(project_id, store_path)


def _pull_via_presign(project_id: str, store_path: Path) -> int:
    """GET /sync/manifest → POST /sync/presign → S3 GETs."""
    from datetime import datetime, timezone

    from nauro.cli.commands.auth import AuthRefreshError
    from nauro.sync.merge import detect_conflict, resolve_conflict, should_skip
    from nauro.sync.remote import (
        PresignError,
        fetch_manifest,
        fetch_via_presigned_url,
        get_via_presigned_url,
        request_presigned_urls,
    )
    from nauro.sync.state import (
        compute_sha256,
        file_changed_locally,
        file_changed_remotely,
        load_state,
        save_state,
        update_file_state,
    )

    typer.echo("Pulling from remote...")

    try:
        manifest = fetch_manifest(project_id)
    except AuthRefreshError as exc:
        typer.echo(f"  {exc}", err=True)
        return 0
    except PresignError as exc:
        logger.exception("Failed to fetch manifest")
        typer.echo(f"  Warning: could not reach remote ({exc})", err=True)
        return 0

    state = load_state(store_path)

    pulls: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str]] = []
    for entry in manifest:
        rel = entry.get("path", "") if isinstance(entry, dict) else ""
        if not rel or should_skip(rel):
            continue
        # Server validates per-op on presign, but the manifest itself is
        # currently trusted — drop suspicious entries before they hit disk.
        if ".." in Path(rel).parts or rel.startswith("/"):
            logger.warning("Skipping manifest entry with suspicious path: %r", rel)
            continue
        remote_etag = entry.get("etag", "")
        if not file_changed_remotely(remote_etag, rel, state):
            continue

        local_file = store_path / rel
        local_changed = file_changed_locally(store_path, rel, state)

        if not local_changed:
            pulls.append((rel, remote_etag))
            continue

        local_sha = compute_sha256(local_file) if local_file.exists() else ""
        if detect_conflict(rel, state, local_sha, remote_etag):
            conflicts.append((rel, remote_etag))

    if not pulls and not conflicts:
        state.last_full_sync = datetime.now(timezone.utc).isoformat()
        save_state(store_path, state)
        typer.echo("  No remote changes")
        return 0

    operations = [{"verb": "GET", "path": rel} for rel, _etag in pulls + conflicts]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        typer.echo(f"  {exc}", err=True)
        return 0
    except PresignError as exc:
        logger.exception("Failed to request presigned URLs")
        typer.echo(f"  Warning: presign request failed ({exc})", err=True)
        return 0

    if len(urls) < len(operations):
        logger.warning("Presign returned %d URLs for %d ops", len(urls), len(operations))

    url_by_path = {
        entry["path"]: entry["url"]
        for entry in urls
        if isinstance(entry, dict) and entry.get("verb") == "GET"
    }
    merged = 0

    for rel, remote_etag in pulls:
        url = url_by_path.get(rel)
        if not url:
            continue
        local_file = store_path / rel
        try:
            get_via_presigned_url(url, local_file)
            local_sha = compute_sha256(local_file)
            update_file_state(state, rel, local_sha, remote_etag)
            merged += 1
        except PresignError:
            logger.exception("Error pulling %s", rel)

    for rel, remote_etag in conflicts:
        url = url_by_path.get(rel)
        if not url:
            continue
        local_file = store_path / rel
        try:
            remote_content = fetch_via_presigned_url(url)
            merged_content = resolve_conflict(store_path, local_file, remote_content, rel, state)
            local_file.write_bytes(merged_content)
            local_sha = compute_sha256(local_file)
            update_file_state(state, rel, local_sha, remote_etag)
            merged += 1
        except PresignError:
            logger.exception("Error resolving conflict for %s", rel)

    state.last_full_sync = datetime.now(timezone.utc).isoformat()
    save_state(store_path, state)

    if merged:
        typer.echo(f"  Merged {merged} file(s) from remote")
    else:
        typer.echo("  No remote changes")

    return merged


def _push_to_cloud(project_id: str, store_path: Path) -> bool:
    """Push store changes after a local sync.

    Cloud-mode projects with a token → presign push. Cloud-mode without
    a token → warn and return False (caller surfaces exit 1). Anything
    else (v1, v2-local) → True since nothing was expected to upload.
    """
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
    return _push_via_presign(project_id, store_path)


def _push_via_presign(project_id: str, store_path: Path) -> bool:
    """POST /sync/presign for changed files → S3 PUTs."""
    from nauro.cli.commands.auth import AuthRefreshError
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

        local_sha = compute_sha256(local_file)
        fs = state.files.get(rel)
        if fs is None or fs.local_sha256 != local_sha:
            changed.append((rel, local_sha, local_file))

    if not changed:
        save_state(store_path, state)
        return True

    operations = [{"verb": "PUT", "path": rel} for rel, _sha, _path in changed]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        typer.echo(f"  {exc}", err=True)
        return False
    except PresignError as exc:
        logger.exception("Failed to request presigned PUT URLs")
        typer.echo(f"  Warning: presign request failed ({exc})", err=True)
        return False

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
    if pushed:
        typer.echo(f"  Pushed {pushed} file(s) to S3")
    return True


def _show_status(project_flag: str | None) -> None:
    """Show cloud sync status — two states only.

    Authenticated → server URL + project-specific last-sync info.
    Not authenticated → guidance to run ``nauro auth login``.
    """
    from nauro.sync.remote import resolve_api_url

    if not load_access_token():
        typer.echo("Sync: not authenticated. Run 'nauro auth login'.")
        return

    typer.echo("Sync: authenticated (presign)")
    typer.echo(f"  Server: {resolve_api_url()}")

    try:
        project_name, store_path = resolve_target_project(project_flag)
    except SystemExit:
        return

    from nauro.sync.state import file_changed_locally, load_state

    state = load_state(store_path)

    typer.echo(f"\nProject: {project_name}")
    typer.echo(f"  Files tracked: {len(state.files)}")
    typer.echo(f"  Last successful sync: {state.last_full_sync or 'never'}")

    pending_local = [rel for rel in state.files if file_changed_locally(store_path, rel, state)]
    if pending_local:
        typer.echo(f"  Pending local changes: {len(pending_local)}")
        for p in pending_local[:5]:
            typer.echo(f"    - {p}")
        if len(pending_local) > 5:
            typer.echo(f"    ... and {len(pending_local) - 5} more")
    else:
        typer.echo("  Pending local changes: none")

    backup_dir = store_path / ".conflict-backup"
    if backup_dir.exists():
        backups = list(backup_dir.iterdir())
        if backups:
            typer.echo(f"  Conflict backups: {len(backups)}")
