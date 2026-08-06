"""nauro log — List recent snapshots with metadata."""

import json
from pathlib import Path

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.reader import _list_decisions
from nauro.store.snapshot import list_snapshots, load_snapshot


def log(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of snapshots to show."),
    full: bool = typer.Option(
        False,
        "--full",
        help="Show complete snapshot content instead of metadata.",
    ),
    all_decisions: bool = typer.Option(
        False,
        "--all",
        help="Show all decisions including superseded ones (only with --decisions).",
    ),
    decisions: bool = typer.Option(
        False,
        "--decisions",
        help="Show decision list instead of snapshots.",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the listing as machine-readable JSON.",
    ),
) -> None:
    """List recent snapshots with version, timestamp, and trigger.

    --decisions lists the decision history instead; --all includes superseded.
    """
    _project_name, store_path = resolve_target_project(project)

    if decisions:
        if json_output:
            _emit_decisions_json(store_path, show_all=all_decisions)
        else:
            _show_decisions(store_path, show_all=all_decisions)
        return

    snapshots = list_snapshots(store_path)

    if json_output:
        shown = snapshots[:limit]
        if full:
            shown = [load_snapshot(store_path, snap["version"]) for snap in shown]
        typer.echo(json.dumps(shown, indent=2))
        return

    if not snapshots:
        typer.echo("No snapshots yet. Run 'nauro sync' to create the first one.")
        return

    shown = snapshots[:limit]

    if full:
        _show_full(store_path, shown)
    else:
        _show_summary(store_path, shown)


def _decision_rows(store_path: Path, *, show_all: bool) -> list[dict]:
    """Decision listing rows, superseded entries filtered unless ``show_all``."""
    return [
        {
            "num": d.num,
            "status": str(d.status.value),
            "version": d.version,
            "title": d.title,
            "superseded_by": d.superseded_by,
        }
        for d in _list_decisions(store_path)
        if show_all or str(d.status.value) != "superseded"
    ]


def _emit_decisions_json(store_path: Path, *, show_all: bool) -> None:
    typer.echo(json.dumps(_decision_rows(store_path, show_all=show_all), indent=2))


def _show_decisions(store_path: Path, show_all: bool = False) -> None:
    """Display decision list with status."""
    all_decs = _list_decisions(store_path)

    if not all_decs:
        typer.echo("No decisions yet.")
        return

    typer.echo(f"{'ID':<8} {'Status':<14} {'V':<4} {'Title'}")
    typer.echo("-" * 70)

    for d in all_decs:
        status = str(d.status.value)
        if not show_all and status == "superseded":
            continue

        if status == "superseded":
            superseded_by = d.superseded_by or "?"
            typer.echo(f"{d.num:03d}      [SUPERSEDED] v{d.version:<3} {d.title} → {superseded_by}")
        else:
            typer.echo(f"{d.num:03d}      active       v{d.version:<3} {d.title}")


def _show_summary(store_path: Path, snapshots: list[dict]) -> None:
    """Display snapshot metadata table."""
    typer.echo(f"{'Version':<10} {'Timestamp':<22} {'Trigger'}")
    typer.echo("-" * 60)

    for snap in snapshots:
        version = f"v{snap['version']:03d}"
        ts = snap["timestamp"][:19].replace("T", " ")
        trigger = snap.get("trigger", "") or "-"

        if len(trigger) > 30:
            trigger = trigger[:27] + "..."

        typer.echo(f"{version:<10} {ts:<22} {trigger}")


def _show_full(store_path: Path, snapshots: list[dict]) -> None:
    """Display full snapshot content."""
    for i, snap_meta in enumerate(snapshots):
        if i > 0:
            typer.echo("\n" + "=" * 60 + "\n")

        snap = load_snapshot(store_path, snap_meta["version"])
        version = f"v{snap['version']:03d}"
        ts = snap["timestamp"][:19].replace("T", " ")
        trigger = snap.get("trigger", "") or "-"

        typer.echo(f"Snapshot {version}  |  {ts}  |  {trigger}")
        typer.echo("-" * 60)

        files = snap.get("files", {})
        for filename in sorted(files):
            typer.echo(f"\n--- {filename} ---")
            typer.echo(files[filename].rstrip())
