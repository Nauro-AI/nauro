"""nauro questions — maintenance commands for open-questions.md.

``migrate`` mints a sequential ``Q###`` id for every legacy
``[YYYY-MM-DD HH:MM UTC]`` entry left over from before the Q-form writer
rollout. It is a one-shot local maintenance command — the parse/format
model and the migration logic live in
:mod:`nauro_core.questions`; this surface only locates the store, drives
the kernel helper, and prints a summary.
"""

import typer
from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.questions import OpenQuestionsFile

from nauro.cli.utils import resolve_target_project
from nauro.store.filesystem_store import FilesystemStore
from nauro.templates.agents_md_regen import warn_then_regen

questions_app = typer.Typer(help="Maintenance commands for open-questions.md.")

_DEFAULT_FILE_BODY = "# Open Questions\n"


@questions_app.command(name="migrate")
def migrate(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the legacy to Q### rename map and write nothing.",
    ),
) -> None:
    """Mint sequential 'Q###' ids for legacy timestamp question entries."""
    project_name, store_path = resolve_target_project(project)
    fs_store = FilesystemStore(store_path)

    content = fs_store.read_file(OPEN_QUESTIONS_MD) or _DEFAULT_FILE_BODY
    result = OpenQuestionsFile.parse(content).migrate()

    if not result.renames:
        typer.echo(f"No legacy question entries to migrate in {project_name}.")
        return

    if dry_run:
        typer.echo(f"Would migrate {len(result.renames)} entry(ies) in {project_name}:")
        for rename in result.renames:
            typer.echo(f"  [{rename.old_id}] -> [{rename.new_id}]  +{rename.logged}")
        typer.echo("Dry run: no changes written.")
        return

    fs_store.write_file(OPEN_QUESTIONS_MD, result.file.format())
    typer.echo(f"Migrated {len(result.renames)} entry(ies) in {project_name}:")
    for rename in result.renames:
        typer.echo(f"  [{rename.old_id}] -> [{rename.new_id}]  +{rename.logged}")
    typer.echo(f"  File: {store_path / OPEN_QUESTIONS_MD}")

    # Refresh AGENTS.md so MCP-disconnected agents see the new ids without a
    # separate `nauro sync`. Mirrors `nauro note`.
    updated_repos = warn_then_regen(
        store_path.name,
        store_path,
        warn=lambda msg: typer.echo(msg, err=True),
    )
    for repo_path in updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")
