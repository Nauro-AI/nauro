"""nauro sync — Capture a snapshot and regenerate AGENTS.md in associated repos."""

from pathlib import Path

import typer

from nauro.cli.commands.auth import load_access_token
from nauro.cli.integrations.outcomes import BridgeOutcome
from nauro.cli.integrations.render import render
from nauro.cli.utils import resolve_target_project
from nauro.store.registry import is_cloud_project
from nauro.store.snapshot import capture_snapshot
from nauro.store.validator import print_warnings, validate_store
from nauro.sync.push import push_store_to_cloud
from nauro.templates.agents_md_regen import warn_then_regen

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

    This is the one command that overwrites an existing AGENTS.md even when
    Nauro did not generate it; a # Manual section survives the rewrite. Every
    other command (setup, adopt, init, attach, note, propose_decision)
    preserves a non-Nauro AGENTS.md and warns instead.

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

    pushed = _push_to_cloud(project_key, store_path)

    if pushed:
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

    Echoes progress to the terminal (warnings on stderr).
    """

    def info(self, msg: str) -> None:
        typer.echo(f"  {msg}")

    def warn(self, msg: str) -> None:
        typer.echo(f"  {msg}", err=True)


def _pull_via_presign(project_id: str, store_path: Path) -> int:
    """GET /sync/manifest → POST /sync/presign → S3 GETs.

    Delegates to the shared pull core with an echo reporter.
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
