"""nauro sync — Capture a snapshot and regenerate AGENTS.md in associated repos."""

import logging
from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.constants import REPO_CONFIG_MODE_CLOUD, REPO_CONFIG_MODE_LOCAL
from nauro.store.registry import (
    RegistrySchemaError,
    get_project_v2,
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


def _project_mode(project_key: str) -> str | None:
    """Return the registered mode for ``project_key`` (cloud/local) or None if unknown.

    Looks up the v2 registry by project_id. v1 entries have no mode field and
    are treated as local-only for sync purposes.
    """
    try:
        v2_entry = get_project_v2(project_key)
    except RegistrySchemaError:
        v2_entry = None
    if v2_entry is None:
        return None
    return v2_entry.get("mode")


def sync(
    message: str = typer.Option("", "--message", "-m", help="Sync message stored in the snapshot."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    cloud_setup: bool = typer.Option(
        False,
        "--cloud-setup",
        help="Interactive wizard to configure S3 sync.",
    ),
    status: bool = typer.Option(False, "--status", help="Show sync status."),
) -> None:
    """Capture a snapshot and regenerate AGENTS.md in each associated repo.

    With cloud sync configured, pulls from S3 first (git-style pull-then-push),
    then pushes the updated store back. Project state in state_current.md is
    not touched — use the MCP `update_state` tool to record what changed.
    """
    if cloud_setup:
        _cloud_setup_wizard()
        return

    if status:
        _show_status(project)
        return

    project_name, store_path = resolve_target_project(project)
    # store_path.name is the project_id under v2 (id-keyed) or name under v1.
    project_key = store_path.name
    trigger = message or "manual sync"

    # Pull from S3 first if cloud sync is configured (git-style pull-then-push)
    _pull_from_cloud(project_key, store_path)

    version = capture_snapshot(store_path, trigger=trigger)

    # Warn about missing repo paths before regenerating
    for repo_str in _registry_repo_paths(project_key):
        if not Path(repo_str).is_dir():
            typer.echo(
                f"  Warning: repo path does not exist, skipping AGENTS.md: {repo_str}\n"
                f"  Fix: remove from registry or update path in ~/.nauro/registry.json",
                err=True,
            )

    # Regenerate AGENTS.md in each associated repo
    updated_repos = regenerate_agents_md_for_project(project_key, store_path)

    pushed = _push_to_cloud(project_key, store_path)

    if pushed:
        typer.echo(f"Synced {project_name} — snapshot v{version:03d}")
        for repo_path in updated_repos:
            typer.echo(f"  Updated AGENTS.md: {repo_path}")
    else:
        typer.echo(
            f"Snapshot v{version:03d} captured locally; not uploaded.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Run store validation and print warnings to stderr
    warnings = validate_store(store_path)
    if warnings:
        print_warnings(warnings)


def _pull_from_cloud(project_name: str, store_path: Path) -> int:
    """Pull remote changes from the server before a local sync.

    Dispatches on registry mode and which transport is configured:

    * v2-local                              → no-op
    * v2-cloud + Auth0 token                → presign path
    * v2-cloud (no token), or v1 with static IAM creds → legacy direct-S3
    * v1 with no static IAM creds           → no-op (preserves pre-PR-B behavior)

    The v1+static-creds fall-through is load-bearing: a v1 user with both
    an Auth0 token and static IAM creds must keep getting their data
    pulled until they re-register through v2 (link --cloud or fresh init).
    The presign path can't help — it requires a server-side ULID record.
    """
    from nauro.cli.commands.auth import load_access_token
    from nauro.sync.config import load_sync_config

    mode = _project_mode(project_name)
    if mode == REPO_CONFIG_MODE_LOCAL:
        return 0
    if mode == REPO_CONFIG_MODE_CLOUD and load_access_token():
        return _pull_via_presign(project_name, store_path)
    if mode == REPO_CONFIG_MODE_CLOUD or load_sync_config().enabled:
        return _pull_via_static_s3(project_name, store_path)
    return 0


def _pull_via_presign(project_id: str, store_path: Path) -> int:
    """New transport: GET /sync/manifest → POST /sync/presign → S3 GETs."""
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
        # Defense in depth — the server validates per-op on presign, but the
        # manifest itself is currently trusted. Drop suspicious entries here.
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


def _pull_via_static_s3(project_name: str, store_path: Path) -> int:
    """Legacy transport: direct S3 with static IAM credentials.

    Removed in PR C (2026-08-15) once the install population has migrated.
    """
    try:
        from nauro.sync.config import AuthRequiredError, load_sync_config, require_auth, s3_prefix
        from nauro.sync.merge import detect_conflict, resolve_conflict, should_skip
        from nauro.sync.remote import create_client, list_remote, pull_file
        from nauro.sync.state import (
            compute_sha256,
            file_changed_locally,
            file_changed_remotely,
            load_state,
            save_state,
            update_file_state,
        )
    except ImportError:
        return 0

    config = load_sync_config()
    if not config.enabled:
        return 0

    try:
        sanitized_sub = require_auth(config)
    except AuthRequiredError as e:
        typer.echo(f"  {e}", err=True)
        return 0

    typer.echo("Pulling from remote...")

    client = create_client(config)
    prefix = s3_prefix(sanitized_sub, project_name)

    try:
        remote_files = list_remote(client, config.bucket_name, prefix)
    except Exception:
        logger.exception("Failed to list remote files")
        typer.echo("  Warning: could not reach remote", err=True)
        return 0

    state = load_state(store_path)
    merged = 0

    for rf in remote_files:
        rel = rf["key"].removeprefix(prefix)
        if not rel or should_skip(rel):
            continue

        remote_etag = rf["etag"]
        remote_changed = file_changed_remotely(remote_etag, rel, state)
        if not remote_changed:
            continue

        local_file = store_path / rel
        local_changed = file_changed_locally(store_path, rel, state)

        if remote_changed and not local_changed:
            # Pull remote version directly
            try:
                remote_key = prefix + rel
                etag = pull_file(client, config.bucket_name, remote_key, local_file)
                local_sha = compute_sha256(local_file)
                update_file_state(state, rel, local_sha, etag)
                merged += 1
            except Exception:
                logger.exception("Error pulling %s", rel)
        elif remote_changed and local_changed:
            # Both changed — conflict resolution
            local_sha = compute_sha256(local_file) if local_file.exists() else ""
            if detect_conflict(rel, state, local_sha, remote_etag):
                try:
                    response = client.get_object(Bucket=config.bucket_name, Key=prefix + rel)
                    remote_content = response["Body"].read()
                    merged_content = resolve_conflict(
                        store_path, local_file, remote_content, rel, state
                    )
                    local_file.write_bytes(merged_content)
                    local_sha = compute_sha256(local_file)
                    update_file_state(state, rel, local_sha, remote_etag)
                    merged += 1
                except Exception:
                    logger.exception("Error resolving conflict for %s", rel)

    from datetime import datetime, timezone

    state.last_full_sync = datetime.now(timezone.utc).isoformat()
    save_state(store_path, state)

    if merged:
        typer.echo(f"  Merged {merged} file(s) from remote")
    else:
        typer.echo("  No remote changes")

    return merged


def _push_to_cloud(project_name: str, store_path: Path) -> bool:
    """Push store changes after a local sync.

    Mirrors the pull dispatch:

    * v2-local                              → True (nothing to push)
    * v2-cloud + Auth0 token                → presign path
    * v2-cloud (no token)                   → legacy direct-S3 (warns + False if no creds)
    * v1 with static IAM creds              → legacy direct-S3
    * v1 with no static IAM creds           → True (preserves pre-PR-B behavior)

    The v1+static-creds fall-through prevents silent push-loss for users
    still on the pre-v2 registry layout.
    """
    from nauro.cli.commands.auth import load_access_token
    from nauro.sync.config import load_sync_config

    mode = _project_mode(project_name)
    if mode == REPO_CONFIG_MODE_LOCAL:
        return True
    if mode == REPO_CONFIG_MODE_CLOUD and load_access_token():
        return _push_via_presign(project_name, store_path)
    if mode == REPO_CONFIG_MODE_CLOUD or load_sync_config().enabled:
        return _push_via_static_s3(project_name, store_path)
    return True


def _push_via_presign(project_id: str, store_path: Path) -> bool:
    """New transport: POST /sync/presign for changed files → S3 PUTs."""
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


def _push_via_static_s3(project_name: str, store_path: Path) -> bool:
    """Legacy transport: direct S3 with static IAM credentials.

    Removed in PR C (2026-08-15) once the install population has migrated.
    """
    try:
        from nauro.sync.config import AuthRequiredError, load_sync_config, require_auth, s3_prefix
        from nauro.sync.merge import should_skip
        from nauro.sync.remote import create_client, push_file
        from nauro.sync.state import compute_sha256, load_state, save_state, update_file_state
    except ImportError:
        typer.echo(
            "  Warning: this is a cloud-mode project but CLI sync credentials are not configured.\n"
            "  Your local snapshot is captured, but nothing was uploaded.\n"
            "  Run 'nauro auth login' or 'nauro sync --cloud-setup' to configure credentials.",
            err=True,
        )
        return False

    config = load_sync_config()
    if not config.enabled:
        typer.echo(
            "  Warning: this is a cloud-mode project but CLI sync credentials are not configured.\n"
            "  Your local snapshot is captured, but nothing was uploaded.\n"
            "  Run 'nauro auth login' or 'nauro sync --cloud-setup' to configure credentials.",
            err=True,
        )
        return False

    try:
        sanitized_sub = require_auth(config)
    except AuthRequiredError as e:
        typer.echo(f"  {e}", err=True)
        return False

    client = create_client(config)
    prefix = s3_prefix(sanitized_sub, project_name)
    state = load_state(store_path)

    pushed = 0
    for local_file in store_path.rglob("*"):
        if not local_file.is_file():
            continue
        try:
            rel = str(local_file.relative_to(store_path))
        except ValueError:
            continue
        if should_skip(rel) or rel.startswith(".conflict-backup") or rel.startswith("__pycache__"):
            continue

        try:
            local_sha = compute_sha256(local_file)
            remote_key = prefix + rel
            new_etag = push_file(client, config.bucket_name, local_file, remote_key)
            if new_etag:
                update_file_state(state, rel, local_sha, new_etag)
                pushed += 1
        except Exception:
            logger.exception("Failed to push %s to S3", rel)

    save_state(store_path, state)
    if pushed:
        typer.echo(f"  Pushed {pushed} file(s) to S3")
    return True


def _cloud_setup_wizard() -> None:
    """Interactive wizard to configure S3 sync credentials."""
    from nauro.store.config import load_config, save_config

    typer.echo("Cloud sync setup — configure AWS S3 credentials\n")
    typer.echo(
        "Note: this wizard configures direct-S3 access. It is being replaced by "
        "server-managed credentials. Future installs will not need this step.\n",
        err=True,
    )

    bucket_name = typer.prompt("S3 bucket name")
    region = typer.prompt("AWS region", default="us-east-1")
    access_key_id = typer.prompt("AWS access key ID")
    secret_access_key = typer.prompt("AWS secret access key", hide_input=True)

    # Validate credentials
    typer.echo("\nValidating credentials...")
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        typer.echo(
            "Error: boto3 is not installed. Install it with: pip install boto3",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        client.head_bucket(Bucket=bucket_name)
        typer.echo("Credentials valid — bucket accessible.")
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code == "403":
            typer.echo(
                "Error: Access denied. Check your credentials and bucket permissions.",
                err=True,
            )
        elif code == "404":
            typer.echo("Error: Bucket not found. Check the bucket name.", err=True)
        else:
            typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"Error connecting to AWS: {e}", err=True)
        raise typer.Exit(code=1)

    # Save to config
    data = load_config()
    data["sync"] = {
        "bucket_name": bucket_name,
        "region": region,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "sync_interval": 30,
    }
    save_config(data)

    typer.echo("\nSync configured. Credentials saved to ~/.nauro/config.json")
    typer.echo("\nNext steps:")
    typer.echo("  1. Run 'nauro sync' to push your project to S3")
    typer.echo("  2. Use 'nauro serve --daemon' to start background sync")
    typer.echo("  3. Run 'nauro sync --status' to check sync state")


def _show_status(project_flag: str | None) -> None:
    """Show cloud sync status."""
    from nauro.cli.commands.auth import load_access_token
    from nauro.sync.config import load_sync_config

    access_token = load_access_token()
    config = load_sync_config()

    if access_token:
        sync_path_label = "presign"
    elif config.enabled:
        sync_path_label = "legacy direct-S3"
    else:
        sync_path_label = "not configured"

    typer.echo(f"Sync path: {sync_path_label}")

    if sync_path_label == "not configured":
        typer.echo(
            "Run 'nauro auth login' to authenticate, "
            "or 'nauro sync --cloud-setup' for legacy direct-S3."
        )
        return

    if config.enabled:
        typer.echo(f"  Bucket: {config.bucket_name}")
        typer.echo(f"  Region: {config.region}")
        typer.echo(f"  Poll interval: {config.sync_interval}s")

    # Show project-specific status if we can resolve one
    try:
        project_name, store_path = resolve_target_project(project_flag)
    except SystemExit:
        return
    project_key = store_path.name

    from nauro.sync.state import load_state

    state = load_state(store_path)

    typer.echo(f"\nProject: {project_name}")
    typer.echo(f"  Files tracked: {len(state.files)}")
    typer.echo(f"  Last successful sync: {state.last_full_sync or 'never'}")

    # Check for pending local changes
    from nauro.sync.state import file_changed_locally

    pending_local = []
    for rel_path in state.files:
        if file_changed_locally(store_path, rel_path, state):
            pending_local.append(rel_path)

    if pending_local:
        typer.echo(f"  Pending local changes: {len(pending_local)}")
        for p in pending_local[:5]:
            typer.echo(f"    - {p}")
        if len(pending_local) > 5:
            typer.echo(f"    ... and {len(pending_local) - 5} more")
    else:
        typer.echo("  Pending local changes: none")

    # Remote-side enumeration is only meaningful for the legacy path; the
    # presign path lists via /sync/manifest at sync time and does not expose
    # the same read-only listing here. Keep the legacy probe for installs
    # still on static IAM creds; PR C removes this block alongside the
    # legacy transport.
    if not access_token and config.enabled:
        try:
            from nauro.sync.config import s3_prefix
            from nauro.sync.remote import create_client, list_remote
            from nauro.sync.state import file_changed_remotely

            client = create_client(config)
            if not (config.user_id or config.sanitized_sub):
                typer.echo("  Remote status unavailable: run 'nauro auth login' first", err=True)
                return
            user_key = config.user_id or config.sanitized_sub
            prefix = s3_prefix(user_key, project_key)
            remote_files = list_remote(client, config.bucket_name, prefix)

            pending_remote = []
            for rf in remote_files:
                rel = rf["key"].removeprefix(prefix)
                if rel and file_changed_remotely(rf["etag"], rel, state):
                    pending_remote.append(rel)

            if pending_remote:
                typer.echo(f"  Pending remote changes: {len(pending_remote)}")
                for p in pending_remote[:5]:
                    typer.echo(f"    - {p}")
            else:
                typer.echo("  Pending remote changes: none")
        except Exception as e:
            typer.echo(f"  Remote check failed: {e}", err=True)

    # Check for conflict backups
    backup_dir = store_path / ".conflict-backup"
    if backup_dir.exists():
        backups = list(backup_dir.iterdir())
        if backups:
            typer.echo(f"  Conflict backups: {len(backups)}")
