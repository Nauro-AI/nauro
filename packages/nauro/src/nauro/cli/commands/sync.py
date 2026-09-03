"""nauro sync — Capture a snapshot and regenerate AGENTS.md in associated repos."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from nauro.auth import load_access_token
from nauro.cli.integrations.outcomes import BridgeOutcome
from nauro.cli.integrations.render import render
from nauro.cli.utils import resolve_target_project
from nauro.store._atomic import is_tmp_sibling
from nauro.store.registry import is_cloud_project
from nauro.store.snapshot import capture_snapshot
from nauro.store.validator import print_warnings, validate_store
from nauro.sync.push import push_store_to_cloud
from nauro.sync.remote import TransferSession, operation_session
from nauro.templates.agents_md_regen import warn_then_regen

if TYPE_CHECKING:
    from nauro.sync.pull import PullReport


def sync(
    message: str = typer.Option("", "--message", "-m", help="Sync message stored in the snapshot."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    status: bool = typer.Option(False, "--status", help="Show sync status."),
) -> None:
    """Capture a snapshot and regenerate AGENTS.md in each associated repo, pulling before pushing
    when cloud sync is configured. The only command that overwrites an unmanaged AGENTS.md, and
    a # Manual section survives. Exits 1 on failure, 2 when the pull left files unwritten.
    """
    if status:
        _show_status(project)
        return

    project_name, store_path = resolve_target_project(project)
    # store_path.name is the project_id (the store directory is id-keyed).
    project_key = store_path.name
    trigger = message or "manual sync"

    with operation_session() as session:
        pulled = _pull_from_cloud(project_key, store_path, session=session)

        version = capture_snapshot(store_path, trigger=trigger)

        # The regen seam ensures the Claude Code bridge wherever AGENTS.md is
        # written (before push, so local artifacts stay consistent even if the push
        # fails); the sink collects those outcomes to echo on the success path.
        bridge_outcomes: list[BridgeOutcome] = []
        updated_repos = warn_then_regen(
            project_key,
            store_path,
            warn=lambda msg: typer.echo(msg, err=True),
            overwrite_unmanaged=True,
            bridge_sink=bridge_outcomes,
        )

        pushed = push_store_to_cloud(project_key, store_path, session=session)

    if pulled.origin_aborted:
        typer.echo(
            f"Error: sync stopped after a permanent remote origin failure for "
            f"{project_name}; snapshot v{version:03d} was captured locally. "
            "Fix the remote connection and run 'nauro sync' again.",
            err=True,
        )
        raise typer.Exit(code=1)

    if pushed.is_complete:
        if is_cloud_project(project_key):
            typer.echo(f"Synced {project_name} - snapshot v{version:03d}")
        else:
            typer.echo(
                f"Captured snapshot v{version:03d} for {project_name}"
                " (local-only project; nothing to upload)."
            )
        for repo_path, bridge_outcome in zip(updated_repos, bridge_outcomes):
            typer.echo(f"  Updated AGENTS.md: {repo_path}")
            for line in render(bridge_outcome):
                typer.echo(line)
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

    if pulled.left_work_behind:
        # Exit 2, after everything else ran: the snapshot, the regen, and the
        # push all succeeded, so this is not the exit-1 failure of the command,
        # but the store does not hold everything the server has and a script
        # must not read that as a clean sync.
        typer.echo(
            f"Error: {_unfinished_pull_detail(pulled)}. The reason is reported "
            "above. Run 'nauro sync' again once the problem is fixed.",
            err=True,
        )
        raise typer.Exit(code=2)


def _unfinished_pull_detail(report: PullReport) -> str:
    """Name what the pull left undone, in the terms that run knows it.
    A run that never read the server's file list has no count to give: zero would claim a store
    level with the server.
    """
    if not report.manifest_read:
        return "this sync could not read the server's file list, so it pulled nothing"
    return f"{report.refused} remote file(s) were not written this sync"


def _pull_from_cloud(
    project_id: str,
    store_path: Path,
    *,
    session: TransferSession | None = None,
) -> PullReport:
    """Pull remote changes via the manifest and presign endpoints.
    A no-op for a non-cloud project or with no Auth0 token: an empty report says the same as a
    run that found nothing.
    """
    from nauro.sync.pull import PullReport

    if not is_cloud_project(project_id):
        return PullReport()
    if not load_access_token():
        return PullReport()
    return _pull_via_presign(project_id, store_path, session=session)


class _EchoReporter:
    """Pull reporter for ``nauro sync``.

    Echoes progress to the terminal (warnings on stderr).
    """

    def info(self, msg: str) -> None:
        typer.echo(f"  {msg}")

    def warn(self, msg: str) -> None:
        typer.echo(f"  {msg}", err=True)


def _pull_via_presign(
    project_id: str,
    store_path: Path,
    *,
    session: TransferSession | None = None,
) -> PullReport:
    """GET /sync/manifest → POST /sync/presign → S3 GETs, via the shared pull core.
    Fails loud when another sync holds the store lock: skipping the pull would push stale
    content over whatever the other run is landing.
    """
    from nauro.sync.lock import SyncLockTimeoutError
    from nauro.sync.pull import run_pull

    typer.echo("Pulling from remote...")
    try:
        return run_pull(project_id, store_path, _EchoReporter(), session=session)
    except SyncLockTimeoutError as exc:
        typer.echo(f"Error: {exc}. Try again once it finishes.", err=True)
        raise typer.Exit(code=1) from exc


def _show_status(project_flag: str | None) -> None:
    """Show cloud sync status — two states only.
    Authenticated prints the server URL and this project's last-sync info; otherwise it points at
    ``nauro auth login``.
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

    from nauro.sync._path_diagnostics import _StoreRootPreparationError
    from nauro.sync.push import plan_push
    from nauro.sync.state import load_state

    state = load_state(store_path)

    typer.echo(f"\nProject: {project_name}")
    typer.echo(f"  Files tracked: {len(state.files)}")
    typer.echo(f"  Last successful sync: {state.last_full_sync or 'never'}")

    try:
        plan = plan_push(store_path, state)
    except _StoreRootPreparationError as exc:
        typer.echo(f"  Pending local changes: unknown ({exc})")
    except OSError as exc:
        typer.echo(f"  Pending local changes: unknown (store scan failed: {exc})")
    else:
        pending_local = [candidate.relative_path for candidate in plan.candidates]
        if pending_local:
            typer.echo(f"  Pending local changes: {len(pending_local)}")
            for p in pending_local[:5]:
                typer.echo(f"    - {p}")
            if len(pending_local) > 5:
                typer.echo(f"    ... and {len(pending_local) - 5} more")
        else:
            typer.echo("  Pending local changes: none")
        if plan.unsafe:
            typer.echo(f"  Unsafe paths skipped: {len(plan.unsafe)}")
            for skipped in plan.unsafe[:5]:
                typer.echo(f"    - {skipped.display}: {skipped.reason}")
            if len(plan.unsafe) > 5:
                typer.echo(f"    ... and {len(plan.unsafe) - 5} more")

    from nauro.sync.merge import CONFLICT_BACKUP_DIR
    from nauro.sync.quarantine import list_quarantine_backups, unresolved_quarantines

    quarantined = unresolved_quarantines(store_path, state)
    if quarantined:
        typer.echo(f"  Quarantined decision-number collisions: {len(quarantined)}")
        for item in quarantined:
            typer.echo(f"    - {item.label} (remote copy: {item.backup_path.name})")

    backup_dir = store_path / CONFLICT_BACKUP_DIR
    if backup_dir.exists():
        # Quarantine backups live in the same directory but are already
        # reported above; counting them again would double-report one event.
        # A tmp sibling stranded by a kill mid-write is not a backup at all,
        # and reporting one would name a conflict that never happened.
        quarantine_names = {item.backup_path.name for item in list_quarantine_backups(store_path)}
        backups = [
            path
            for path in backup_dir.iterdir()
            if path.name not in quarantine_names and not is_tmp_sibling(path.name)
        ]
        if backups:
            typer.echo(f"  Conflict backups: {len(backups)}")
