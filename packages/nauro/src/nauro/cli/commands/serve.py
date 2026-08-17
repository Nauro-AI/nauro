"""nauro serve — Start the local MCP server over stdio.

Claude Code (and other MCP clients) spawn this process and communicate over
stdin/stdout. stdio is the sole supported local transport; the former local
FastAPI HTTP transport has been retired.

The server resolves project context from the cwd's ``.nauro/config.json``;
there is no ``--project`` flag — one source of truth.
"""

import typer


def serve(
    stdio: bool = typer.Option(
        True,
        "--stdio",
        help="Run over stdio (the only supported transport).",
        hidden=True,
    ),
) -> None:
    """Start the Nauro MCP server over stdio.
    '--stdio' is a no-op, accepted for client configs that still spawn 'nauro serve --stdio'.
    """
    from nauro.mcp.stdio_server import run_stdio

    run_stdio()
