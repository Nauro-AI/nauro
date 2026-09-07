"""nauro serve — Start the local MCP server over stdio.

Claude Code (and other MCP clients) spawn this process and communicate over
stdin/stdout. stdio is the sole supported local transport; the former local
FastAPI HTTP transport has been retired.

The server resolves project context from the cwd's ``.nauro/config.json``;
there is no ``--project`` flag. Explicit reference mode instead uses its operator profile.
"""

from pathlib import Path

import typer


def serve(
    stdio: bool = typer.Option(
        True,
        "--stdio",
        help="Run over stdio (the only supported transport).",
        hidden=True,
    ),
    reference_profile: Path | None = typer.Option(
        None,
        "--reference-profile",
        help="Start a decision-only reference connection using an explicit operator profile.",
    ),
) -> None:
    """Start the Nauro MCP server over stdio.
    '--stdio' is a no-op, accepted for client configs that still spawn 'nauro serve --stdio'.
    """
    if reference_profile is not None:
        from nauro.mcp.decision_reference_startup import ReferenceStartupError, run_reference_stdio

        try:
            run_reference_stdio(reference_profile)
        except ReferenceStartupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        return

    from nauro.mcp.stdio_server import run_stdio

    run_stdio()
