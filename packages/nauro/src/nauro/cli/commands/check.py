"""nauro check — Run check_decision from the shell, no MCP wiring required.

This is the L1 surface in the progressive-enhancement onboarding model: an
agent or developer with the ``nauro`` CLI installed can run conflict-detection
in the current session without restarting their MCP client. The output is the
same retrieval the MCP ``check_decision`` tool returns; nothing is written.

CLI invocations skip the ``@mcp_tool`` decorator's telemetry side-effects by
calling :func:`nauro.mcp.tools.compute_check_decision` directly — the Typer
instrumentation already emits ``cli.command_invoked`` for every command.
"""

from __future__ import annotations

import json

import typer

from nauro.cli.utils import resolve_target_project
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.mcp.tools import compute_check_decision
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
    result: dict,
) -> str:
    """Render the check_decision result as human-readable terminal output."""
    lines: list[str] = []
    lines.append(f"store:    {result.get('store', 'local')}")
    lines.append(f"project:  {project_name}")
    lines.append(f"approach: {approach}")
    lines.append("")

    related = result.get("related_decisions", [])
    if not related:
        lines.append(result.get("assessment", "No related decisions found."))
        return "\n".join(lines)

    lines.append(f"Related decisions ({len(related)}):")
    for d in related:
        did = d.get("id", "?")
        title = d.get("title", "")
        score = d.get("score", 0.0)
        status = d.get("status", "")
        date = d.get("date", "")
        lines.append(f"  {did}  {title}  (score {score:.1f}, status {status}, decided {date})")

    lines.append("")
    lines.append(result.get("assessment", ""))
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

    # Cloud-mode notice. We always read local data; the warning goes to stderr
    # so --json output stays parseable.
    if _is_cloud_project(store_path.name):
        typer.echo(
            f"Note: project '{project_name}' syncs to cloud; CLI check uses "
            "the local copy. Run `nauro sync` first if it might be stale.",
            err=True,
        )

    result = compute_check_decision(store_path, approach)
    # Any non-empty status field is an error-shaped response from compute_check_decision
    # (currently "error" for missing store, "rejected" for over-length input).
    # Both human and JSON paths must exit 1 on those — a script piping --json
    # and checking $? would otherwise silently treat input-too-long as success.
    is_error = result.get("status") in ("error", "rejected")

    if json_output:
        typer.echo(json.dumps(result, indent=2))
        if is_error:
            raise typer.Exit(code=1)
        return

    if result.get("status") == "error":
        typer.echo(result.get("guidance", "Error"), err=True)
        raise typer.Exit(code=1)
    if result.get("status") == "rejected":
        typer.echo(result.get("reason", "Rejected"), err=True)
        raise typer.Exit(code=1)

    typer.echo(_render_human(project_name, approach, str(store_path), result))
