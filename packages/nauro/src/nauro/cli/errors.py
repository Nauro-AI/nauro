"""Friendly rendering for uncaught filesystem errors.

Commands that write to the store (``note``, ``propose-decision``,
``update-state``, ``flag-question``, ``sync``) or scaffold it (``init``) can hit
a read-only store, a read-only ``NAURO_HOME``, a full disk, or a
permission-denied path. Left unhandled, the ``OSError`` reaches Typer as a raw
traceback with absolute paths. ``apply_fs_error_handling`` wraps every command
so such an error renders as a clean one-line message and exits 1.

This is applied after command registration so every command receives the same
filesystem error handling.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import typer


def _render_and_exit(exc: OSError) -> None:
    """Print a clean one-line message for *exc* and exit 1 (no traceback)."""
    detail = exc.strerror or str(exc) or exc.__class__.__name__
    # For a rename failure (os.replace of a tmp sibling, as atomic_write_text
    # uses) filename is the temp name and filename2 the real destination —
    # prefer the destination so the message names the file the user cares about.
    target = getattr(exc, "filename2", None) or getattr(exc, "filename", None)
    suffix = f": {target}" if target else ""
    typer.echo(f"Error: {detail}{suffix}", err=True)
    raise typer.Exit(code=1)


def friendly_fs_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a command callback to convert an uncaught ``OSError`` into a clean exit.

    Idempotent. Control-flow exceptions (``typer.Exit``/``Abort``/``SystemExit``)
    pass through untouched; only a genuine filesystem ``OSError`` that no command
    handled is rendered.
    """
    if getattr(func, "_nauro_fs_wrapped", False):
        return func

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (typer.Exit, typer.Abort, SystemExit):
            raise
        except OSError as exc:
            _render_and_exit(exc)

    wrapper._nauro_fs_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def apply_fs_error_handling(app: typer.Typer) -> None:
    """Recursively wrap every command in ``app`` (and its sub-Typers)."""
    for cmd_info in app.registered_commands:
        if cmd_info.callback is None:
            continue
        cmd_info.callback = friendly_fs_errors(cmd_info.callback)
    for group_info in app.registered_groups:
        sub = group_info.typer_instance
        if sub is None:
            continue
        apply_fs_error_handling(sub)
