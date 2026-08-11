"""Terminal progress surfaces shared by the commands that run transfers.

Both ``attach`` and ``reconnect`` print their result to stdout and their
progress to stderr, so a caller reading the result of the command never has to
filter the running commentary out of it.
"""

from __future__ import annotations

import typer


class StderrReporter:
    """Reporter that keeps transfer progress off stdout."""

    def info(self, msg: str) -> None:
        typer.echo(f"  {msg}", err=True)

    def warn(self, msg: str) -> None:
        typer.echo(f"  {msg}", err=True)


__all__ = ["StderrReporter"]
