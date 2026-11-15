"""Friendly rendering for uncaught filesystem and registry schema errors.

Commands that write to the store (``note``, ``propose-decision``,
``update-state``, ``flag-question``, ``sync``) or scaffold it (``init``) can hit
a read-only store, a read-only ``NAURO_HOME``, a full disk, or a
permission-denied path. Left unhandled, the ``OSError`` reaches Typer as a raw
traceback with absolute paths. A registry written by an incompatible nauro
raises ``RegistrySchemaError``, whose migration or upgrade hint must render
cleanly too. ``apply_fs_error_handling`` wraps every command and group callback
so these errors render as a clean one-line message and exit 1.

This is applied after command registration so every command receives the same
filesystem error handling.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import typer

from nauro.store.registry import RegistrySchemaError


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
    """Wrap a command callback so an uncaught error becomes a clean exit.
    Idempotent. ``typer.Exit``, ``Abort`` and ``SystemExit`` pass through untouched.
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
        except RegistrySchemaError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None

    wrapper._nauro_fs_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def apply_fs_error_handling(app: typer.Typer) -> None:
    """Recursively wrap every command and group callback in ``app``."""
    for cmd_info in app.registered_commands:
        if cmd_info.callback is None:
            continue
        cmd_info.callback = friendly_fs_errors(cmd_info.callback)
    if app.registered_callback is not None and app.registered_callback.callback is not None:
        app.registered_callback.callback = friendly_fs_errors(app.registered_callback.callback)
    for group_info in app.registered_groups:
        sub = group_info.typer_instance
        if sub is None:
            continue
        apply_fs_error_handling(sub)
