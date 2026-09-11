"""Route existing read and sync commands for migrated local replicas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from nauro.mcp.read_dispatch import GENERATION_READS, generation_response
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import resolve_project_binding
from nauro.sync.generation_refresh_status import refresh_replica, replica_status
from nauro.sync.remote import TransferBoundaryError


def read_command(name: str, project: str, options: dict[str, Any], *, text: bool) -> bool:
    if name not in GENERATION_READS:
        return False
    try:
        binding = resolve_project_binding(project, None, use_cwd=False)
        if observe_generation_marker(binding) is None:
            return False
        response = generation_response(binding, name, options)
        typer.echo(response.text if text else json.dumps(response.envelope, ensure_ascii=False))
        if response.is_error:
            raise typer.Exit(1)
    except (GenerationAuthorityError, TransferBoundaryError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    return True


def refresh_command(project: str, *, push_only: bool) -> bool:
    try:
        binding = resolve_project_binding(project, None, use_cwd=False)
        if observe_generation_marker(binding) is None:
            return False
        if push_only:
            typer.echo("Error: Generation replicas cannot push local files.", err=True)
            raise typer.Exit(1)
        store = refresh_replica(binding)
        typer.echo(f"Refreshed generation {store.target.identity.generation_id}.")
    except (GenerationAuthorityError, TransferBoundaryError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    return True


def require_legacy_read(name: str, store_path: Path) -> None:
    if name not in GENERATION_READS:
        return
    try:
        binding = resolve_project_binding(store_path.name, None, use_cwd=False)
        if binding.store_path != store_path or observe_generation_marker(binding) is not None:
            raise GenerationAuthorityError("Project read authority changed during the read.")
    except (ValueError, GenerationAuthorityError, OSError) as exc:
        typer.echo("Error: Project read authority changed during the read.", err=True)
        raise typer.Exit(1) from exc


def status_command(project: str, *, json_output: bool) -> bool:
    try:
        binding = resolve_project_binding(project, None, use_cwd=False)
        if observe_generation_marker(binding) is None:
            return False
        status = replica_status(binding)
    except (ValueError, OSError):
        status = {"project_id": project, "error_code": "replica_status_unavailable"}
    if json_output:
        typer.echo(json.dumps({"replica_status": status}))
    else:
        typer.echo(f"Project: {project}")
        typer.echo(f"Generation: {status.get('generation_id') or 'unavailable'}")
        typer.echo(
            f"Last refresh attempt: {status.get('last_refresh_attempt_at') or 'not recorded'}"
        )
        typer.echo(
            f"Last successful refresh: {status.get('last_refresh_succeeded_at') or 'not recorded'}"
        )
        error = status.get("error_code") or status.get("last_refresh_error_code")
        if error:
            typer.echo(f"Refresh: {error}. Check 'nauro auth status', then run 'nauro sync'.")
        typer.echo("Local diagnostic status. Current server authorization was not checked.")
    if status.get("error_code"):
        raise typer.Exit(1)
    return True
