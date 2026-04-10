"""nauro serve — Start the local MCP server.

Supports two transports:
  --stdio   Stdio transport (default for Claude Code integration).
            Claude Code spawns this process and communicates over stdin/stdout.
  (default) HTTP transport on localhost:7432. For advanced use or other tools.
"""

import multiprocessing

import typer

from nauro.constants import DEFAULT_MCP_PORT, MCP_HOST


def serve(
    port: int = typer.Option(
        DEFAULT_MCP_PORT,
        "--port",
        "-p",
        help="Port to listen on (HTTP mode).",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        "-d",
        help="Run HTTP server in background.",
    ),
    stdio: bool = typer.Option(
        False,
        "--stdio",
        help="Run over stdio (for Claude Code MCP integration).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
) -> None:
    """Start the Nauro MCP server."""
    if stdio:
        from nauro.mcp.stdio_server import run_stdio

        run_stdio()
        return

    import uvicorn

    if daemon:
        proc = multiprocessing.Process(
            target=uvicorn.run,
            args=("nauro.mcp.server:app",),
            kwargs={"host": MCP_HOST, "port": port, "log_level": "info"},
            daemon=True,
        )
        proc.start()
        typer.echo(
            f"Nauro MCP server started in background on http://{MCP_HOST}:{port} (pid {proc.pid})"
        )

    else:
        typer.echo(f"Starting Nauro MCP server on http://{MCP_HOST}:{port} (Ctrl+C to stop)")
        uvicorn.run("nauro.mcp.server:app", host=MCP_HOST, port=port, log_level="info")
