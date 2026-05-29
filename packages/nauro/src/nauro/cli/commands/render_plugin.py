"""nauro render-plugin — render the bundled subagents into a plugin tree.

Hidden command that materializes the canonical ``nauro-*`` subagent bodies
into ``<dir>/agents/`` so a separate plugin repo can commit byte-identical
copies rendered from one source. ``--check`` byte-verifies the committed
copies against the live render, which is the cross-repo byte-identity gate
run in the plugin repo's CI.
"""

from __future__ import annotations

from pathlib import Path

import typer

from nauro.agents import AGENT_NAMES, emit_plugin_agents, render_agent


def render_plugin(
    dir: Path,
    check: bool = typer.Option(
        False,
        "--check",
        help="Verify committed agents match the live render; exit 1 on any drift.",
    ),
) -> None:
    """Render or verify the bundled subagents under ``<dir>/agents/``."""
    if check:
        for name in AGENT_NAMES:
            target = dir / "agents" / f"{name}.md"
            expected = render_agent("claude_code", name)
            if not target.is_file() or target.read_text(encoding="utf-8") != expected:
                typer.echo(f"drift: {target}", err=True)
                typer.echo(f"re-run: nauro render-plugin {dir}", err=True)
                raise typer.Exit(code=1)
        typer.echo("OK: rendered agents match the bundled source")
        return

    for path in emit_plugin_agents(dir):
        typer.echo(str(path))
