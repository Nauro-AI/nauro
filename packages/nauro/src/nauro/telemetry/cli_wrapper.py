"""Typer command instrumentation for cli.command_invoked telemetry.

instrument_app() walks a Typer app's registered_commands and registered_groups
recursively and wraps every command callback with a timing/success-tracking
shim that emits exactly one cli.command_invoked event per invocation.

Wrapping happens once at app construction (in cli/main.py) so individual
command modules stay free of telemetry imports — no per-command code per
the T1.4 spec.
"""

from __future__ import annotations

import functools
import platform
import time
from collections.abc import Callable
from importlib import metadata
from typing import Any

import typer

from nauro.telemetry import capture
from nauro.telemetry._buckets import bucket as _bucket
from nauro.telemetry.events import cli_command_invoked


def _resolve_version() -> str:
    try:
        return metadata.version("nauro")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


_NAURO_VERSION = _resolve_version()
_OS = platform.system()


def instrument(func: Callable[..., Any], *, command_path: str) -> Callable[..., Any]:
    """Wrap a Typer command callback to emit cli.command_invoked exactly once.

    Idempotent: returns ``func`` unchanged if it has already been wrapped.
    """
    if getattr(func, "_nauro_instrumented", False):
        return func

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        success = True
        try:
            return func(*args, **kwargs)
        except typer.Exit as exc:
            success = exc.exit_code == 0
            raise
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            success = code == 0
            raise
        except BaseException:
            success = False
            raise
        finally:
            try:
                capture(
                    "cli.command_invoked",
                    cli_command_invoked(
                        command=command_path,
                        success=success,
                        duration_bucket=_bucket(time.perf_counter() - start),
                        nauro_version=_NAURO_VERSION,
                        os_name=_OS,
                    ),
                )
            except Exception:
                pass

    wrapper._nauro_instrumented = True  # type: ignore[attr-defined]
    return wrapper


def instrument_app(app: typer.Typer, *, prefix: tuple[str, ...] = ()) -> None:
    """Recursively wrap every registered command in ``app`` and its sub-Typers.

    The dotted command path (e.g. ``"telemetry.disable"``) is captured at
    registration time and passed into ``instrument`` as a closure argument.
    """
    for cmd_info in app.registered_commands:
        if cmd_info.callback is None:
            continue
        name = cmd_info.name or cmd_info.callback.__name__.replace("_", "-")
        cmd_info.callback = instrument(
            cmd_info.callback,
            command_path=".".join((*prefix, name)),
        )
    for group_info in app.registered_groups:
        sub = group_info.typer_instance
        if sub is None or group_info.name is None:
            continue
        instrument_app(sub, prefix=(*prefix, group_info.name))
