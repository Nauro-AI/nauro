"""nauro sync — Capture a snapshot and regenerate AGENTS.md in associated repos."""

import contextlib
import logging
import time
from pathlib import Path

import typer

from nauro.cli.commands.auth import load_access_token
from nauro.cli.utils import resolve_target_project
from nauro.constants import SNAPSHOTS_DIR
from nauro.store.registry import is_cloud_project
from nauro.store.snapshot import capture_snapshot, list_snapshots
from nauro.store.validator import print_warnings, validate_store
from nauro.sync.push import push_store_to_cloud
from nauro.telemetry import capture
from nauro.telemetry._buckets import bucket, byte_bucket
from nauro.telemetry.events import sync_completed
from nauro.templates.agents_md_regen import warn_then_regen

logger = logging.getLogger("nauro.sync")

# Names retained for callers/tests that import the push helper from this
# command module; the implementation now lives in ``nauro.sync.push``.
_push_to_cloud = push_store_to_cloud


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
    state_current.md is not touched — use the MCP 'update_state' tool to
    record what changed. After a successful sync, structural store
    validation runs and any warnings are printed at the end.
    """
    if status:
        _show_status(project)
        return

    project_name, store_path = resolve_target_project(project)
    # store_path.name is the project_id under v2 (id-keyed) or name under v1.
    project_key = store_path.name
    trigger = message or "manual sync"

    _pull_from_cloud(project_key, store_path)

    _capture_start = time.perf_counter()
    version = capture_snapshot(store_path, trigger=trigger)
    _emit_sync_completed(store_path, version, time.perf_counter() - _capture_start)

    updated_repos = warn_then_regen(
        project_key,
        store_path,
        warn=lambda msg: typer.echo(msg, err=True),
    )

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
            f"Error: cloud push failed for {project_name}; snapshot v{version:03d} "
            "was captured locally and will be pushed on the next successful sync.",
            err=True,
        )
        raise typer.Exit(code=1)

    warnings = validate_store(store_path)
    if warnings:
        print_warnings(warnings)


def _emit_sync_completed(store_path: Path, version: int, elapsed: float) -> None:
    """Emit one `sync.completed` telemetry event for a just-captured snapshot.

    Properties are coarsened to magnitude buckets (PRIVACY.md taxonomy):
    snapshot_count is the post-capture snapshot total, duration_bucket times the
    capture, and bytes_bucket coarsens the written snapshot's on-disk size.
    Emission is gated inside capture() by _should_emit(); the surrounding
    suppress keeps a metric-computation failure from ever breaking `nauro sync`.
    """
    with contextlib.suppress(Exception):
        snapshot_count = len(list_snapshots(store_path))
        snapshot_path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
        size = snapshot_path.stat().st_size
        capture(
            "sync.completed",
            sync_completed(
                snapshot_count=snapshot_count,
                duration_bucket=bucket(elapsed),
                bytes_bucket=byte_bucket(size),
            ),
        )


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


class _EchoReporter:
    """Pull reporter for ``nauro sync``.

    Echoes progress to the terminal (warnings on stderr) and re-raises on a
    union-merge failure so an explicit sync fails loud rather than reporting a
    partial success.
    """

    def info(self, msg: str) -> None:
        typer.echo(f"  {msg}")

    def warn(self, msg: str) -> None:
        typer.echo(f"  {msg}", err=True)

    def on_merge_failure(self, relative_path: str, exc: Exception) -> bool:
        logger.exception("Union merge failed for %s", relative_path)
        typer.echo(
            f"  Error: merge failed for {relative_path} ({exc}) — left unchanged",
            err=True,
        )
        return True


def _pull_via_presign(project_id: str, store_path: Path) -> int:
    """GET /sync/manifest → POST /sync/presign → S3 GETs.

    Delegates to the shared pull core with an echo reporter; a union-merge
    failure propagates so ``nauro sync`` exits nonzero.
    """
    from nauro.sync.pull import run_pull

    typer.echo("Pulling from remote...")
    return run_pull(project_id, store_path, _EchoReporter())


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
    except typer.Exit:
        # resolve_target_project raises typer.Exit (a RuntimeError, not a
        # SystemExit, so the previous `except SystemExit` never caught it). When
        # no project resolves from the cwd, swallow it so --status stays a clean
        # two-state report instead of erroring out. An explicit --project that
        # fails to resolve is a real error, though, so its message and a nonzero
        # exit must agree: re-raise that case.
        if project_flag is not None:
            raise
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
