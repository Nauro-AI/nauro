"""nauro check — Run check_decision from the shell, no MCP wiring required.

This is the L1 surface in the progressive-enhancement onboarding model: an
agent or developer with the ``nauro`` CLI installed can run conflict-detection
in the current session without restarting their MCP client. The output is the
same retrieval the MCP ``check_decision`` tool returns; nothing is written.

CLI invocations call the kernel operation directly so the ``@mcp_tool``
decorator's ``mcp.tool_called`` event never fires from the CLI surface;
the Typer instrumentation already emits ``cli.command_invoked``.
"""

from __future__ import annotations

import json

import typer
from nauro_core.operations import CheckDecisionResult, check_decision

from nauro.cli.utils import resolve_target_project
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.onboarding import WELCOME_NO_PROJECT
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import RegistrySchemaError, get_project_v2


def _is_cloud_project(project_key: str) -> bool:
    """Return True iff the v2 registry marks this project as cloud-mode.

    Used only to surface a "may be stale" notice — CLI check always reads
    the local store regardless. v1 projects (no mode field) are treated as
    local; cloud-mode CLI check is deferred until the local surface settles.
    """
    try:
        entry = get_project_v2(project_key)
    except RegistrySchemaError:
        return False
    if entry is None:
        return False
    return entry.get("mode") == REPO_CONFIG_MODE_CLOUD


def _render_human(
    project_name: str,
    approach: str,
    store_path_str: str,
    result: CheckDecisionResult,
) -> str:
    """Render the check_decision result as human-readable terminal output."""
    lines: list[str] = []
    lines.append("store:    local")
    lines.append(f"project:  {project_name}")
    lines.append(f"approach: {approach}")
    lines.append("")

    if not result.related_decisions:
        lines.append(result.assessment or "No related decisions found.")
        return "\n".join(lines)

    lines.append(f"Related decisions ({len(result.related_decisions)}):")
    for d in result.related_decisions:
        lines.append(
            f"  {d.id}  {d.title}  (score {d.score:.1f}, status {d.status}, decided {d.date})"
        )

    lines.append("")
    lines.append(result.assessment)
    lines.append("")
    lines.append(
        f"For full rationale, read decision files in {store_path_str}/decisions/, "
        "or call the get_decision MCP tool after `nauro setup` + restart."
    )
    return "\n".join(lines)


def check(
    approach: str = typer.Argument(..., help="Proposed approach to check."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project name or id (default: resolve from cwd).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the raw result dict as JSON instead of human-readable output.",
    ),
) -> None:
    """Check a proposed approach against the project's decision log.

    Returns the same retrieval as the ``check_decision`` MCP tool. Nothing is
    written. Use this from a shell to demo conflict-detection without first
    wiring MCP and restarting an agent — the L1 bridge in Nauro's progressive
    onboarding model.
    """
    project_name, store_path = resolve_target_project(project)

    # Missing store is a CLI-side concern: the operation expects a live Store,
    # so we surface the onboarding hint before constructing one.
    if not store_path.exists():
        envelope = {
            "store": "local",
            "status": "error",
            "guidance": WELCOME_NO_PROJECT,
        }
        if json_output:
            typer.echo(json.dumps(envelope, indent=2))
        else:
            typer.echo(WELCOME_NO_PROJECT, err=True)
        raise typer.Exit(code=1)

    # Cloud-mode notice. We always read local data; the warning goes to stderr
    # so --json output stays parseable.
    if _is_cloud_project(store_path.name):
        typer.echo(
            f"Note: project '{project_name}' syncs to cloud; CLI check uses "
            "the local copy. Run `nauro sync` first if it might be stale.",
            err=True,
        )

    result = check_decision(FilesystemStore(store_path), approach)

    if json_output:
        envelope = {"store": "local", **result.model_dump(mode="json", exclude_none=True)}
        typer.echo(json.dumps(envelope, indent=2))
        if result.error is not None:
            raise typer.Exit(code=1)
        return

    if result.error is not None:
        typer.echo(result.error.reason, err=True)
        raise typer.Exit(code=1)

    typer.echo(_render_human(project_name, approach, str(store_path), result))
