"""nauro journal: emit the write-path provenance journal as JSON on stdout.

Reads the store-local D455 event journal and writes every parseable event as a
single JSON array to stdout, oldest first (append order). It is the machine-read
the D456 desktop viewer's operations log consumes; it is deliberately a
hand-written CLI command, not an MCP tool, because it serves a local GUI read
and does not belong on the frozen stdio tool contract.

The reader tolerates a truncated or corrupt final record by skipping it, so a
crash mid-append never denies the whole log. A missing or empty journal emits an
empty array and exits 0. Windowing and filtering are display concerns the app
owns, so the command carries only ``--project``.
"""

from __future__ import annotations

import json

import typer

from nauro.cli.utils import resolve_target_project
from nauro.store.journal import read_events


def journal(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Emit the project's write-path provenance journal as a JSON array on stdout."""
    _project_name, store_path = resolve_target_project(project)

    events = read_events(store_path)
    records = [event.model_dump(mode="json", exclude_none=True) for event in events]
    typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
