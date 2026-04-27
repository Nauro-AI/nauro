"""nauro sync — Capture a snapshot and update state."""

import logging
from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.registry import (
    RegistrySchemaError,
    get_project_v2,
    load_registry,
)
from nauro.store.snapshot import capture_snapshot
from nauro.store.validator import print_warnings, validate_store
from nauro.store.writer import update_state
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
    cloud_setup: bool = typer.Option(
        False,
        "--cloud-setup",
        help="Interactive wizard to configure S3 sync.",
    ),
    status: bool = typer.Option(False, "--status", help="Show sync status."),
) -> None:
    """Capture a snapshot and update the project state."""
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
    update_state(store_path, f"Snapshot v{version:03d}: {trigger}")

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

    typer.echo(f"Synced {project_name} — snapshot v{version:03d}")
    for repo_path in updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")

    # Push to S3 if sync is configured
    _push_to_cloud(project_key, store_path)

    # Run store validation and print warnings to stderr
    warnings = validate_store(store_path)
    if warnings:
        print_warnings(warnings)


def _pull_from_cloud(project_name: str, store_path: Path) -> int:
    """Pull remote changes from S3 before a local sync. Skip silently if not configured.

    Returns the number of files merged/pulled, or 0 if nothing changed.
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

    from datetime import UTC, datetime

    state.last_full_sync = datetime.now(UTC).isoformat()
    save_state(store_path, state)

    if merged:
        typer.echo(f"  Merged {merged} file(s) from remote")
    else:
        typer.echo("  No remote changes")

    return merged


def _push_to_cloud(project_name: str, store_path: Path) -> None:
    """Push all store files to S3 after a local sync. Skip silently if not configured."""
    try:
        from nauro.sync.config import AuthRequiredError, load_sync_config, require_auth, s3_prefix
        from nauro.sync.merge import should_skip
        from nauro.sync.remote import create_client, push_file
        from nauro.sync.state import compute_sha256, load_state, save_state, update_file_state
    except ImportError:
        return

    config = load_sync_config()
    if not config.enabled:
        return

    try:
        sanitized_sub = require_auth(config)
    except AuthRequiredError as e:
        typer.echo(f"  {e}", err=True)
        return

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


def _cloud_setup_wizard() -> None:
    """Interactive wizard to configure S3 sync credentials."""
    from nauro.store.config import load_config, save_config

    typer.echo("Cloud sync setup — configure AWS S3 credentials\n")

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
    from nauro.sync.config import load_sync_config

    config = load_sync_config()

    if not config.enabled:
        typer.echo("Cloud sync: disabled")
        typer.echo("Run 'nauro sync --cloud-setup' to configure.")
        return

    typer.echo("Cloud sync: enabled")
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
    typer.echo(f"  Last full sync: {state.last_full_sync or 'never'}")

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

    # Check for pending remote changes
    try:
        from nauro.sync.config import s3_prefix
        from nauro.sync.remote import create_client, list_remote
        from nauro.sync.state import file_changed_remotely

        client = create_client(config)
        if not (config.user_id or config.sanitized_sub):
            typer.echo("  Remote status unavailable: run 'nauro auth login' first", err=True)
            return 0
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
