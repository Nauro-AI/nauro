"""Resolve and validate the durable nauro command for recorded MCP/hook wiring."""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from nauro.cli._codex_hooks import _CODEX_HOOK_PROBE_ARGS


def probe_nauro_command(
    cmd: str,
    *,
    args: tuple[str, ...] = ("--version",),
    timeout: float = 1.5,
) -> bool:
    """Return True iff ``[cmd, *args]`` launches and exits 0.
    A launch failure, a hang past ``timeout``, and a non-zero exit all count as "won't run".
    Soft-fails and never raises, so callers can treat the boolean as authoritative.
    """
    try:
        proc = subprocess.run(
            [cmd, *args],
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


_DURABLE_PATH_MARKERS: tuple[tuple[str, str], ...] = (("pipx", "venvs"), ("uv", "tools"))
_FRAGILE_VENV_DIRS = frozenset({".venv", "venv", "env"})


def _is_durable_install_path(path: str) -> bool:
    """Heuristic: does ``path`` look like a durable, tool-managed install?
    pipx and uv-tool layouts are durable, a project-local ``.venv``/``venv``/``env`` is fragile,
    and every other shape counts as durable. A tiebreaker only: a fragile path is still used.
    """
    parts = [p.lower() for p in Path(path).parts]
    for first, second in _DURABLE_PATH_MARKERS:
        for i in range(len(parts) - 1):
            if parts[i] == first and parts[i + 1] == second:
                return True
    if len(parts) >= 3 and parts[-3] in _FRAGILE_VENV_DIRS:
        return False
    return True


def _interpreter_sibling_candidate() -> str | None:
    """Return the absolute path to a ``nauro`` console script beside the running interpreter.
    ``None`` when there is none. The absolute path keeps the spawned stdio server and the
    per-turn hook independent of the agent's launch PATH, which GUI launches leave bare.
    """
    bindir = Path(sys.executable).parent
    for name in ("nauro", "nauro.exe"):
        candidate = bindir / name
        if candidate.is_file():
            return str(candidate)
    return None


_FRAGILE_COMMAND_WARNING = (
    "WARNING: recording nauro from a project virtualenv ({command}).\n"
    "  This path breaks if the repo's virtualenv is rebuilt, moved, or "
    "corrupted, silently killing Nauro's MCP server and hooks. Install nauro "
    "durably (pipx install nauro, or uv tool install nauro) and re-run "
    "'nauro setup all'."
)

_UNRESOLVED_COMMAND_WARNING = (
    "WARNING: could not validate a working nauro; recorded '{command}'.\n"
    "  Nauro's MCP server and hooks will not work until nauro is installed on a "
    "durable PATH (pipx install nauro, or uv tool install nauro), then re-run "
    "'nauro setup all'."
)


@functools.cache
def _find_nauro_command() -> str:
    """Resolve and process-cache the nauro entrypoint recorded into MCP and hook configs.
    Cached so ``setup all`` probes once rather than once per sink; warnings surface only on the
    cache-miss resolution. Tests reset with ``_find_nauro_command.cache_clear()``.
    """
    return _resolve_nauro_command()


def _resolve_nauro_command() -> str:
    """Pick the nauro entrypoint to record into MCP and hook configs.
    Prefers an interpreter-sibling that runs and looks durable, else a durable PATH shim, else
    the sibling with a fragility warning, else the best absolute path or bare ``nauro``.
    """
    sibling = _interpreter_sibling_candidate()
    which = shutil.which("nauro")

    if sibling is not None and _is_durable_install_path(sibling) and probe_nauro_command(sibling):
        return sibling

    if which is not None and _is_durable_install_path(which) and probe_nauro_command(which):
        return which

    if sibling is not None and probe_nauro_command(sibling):
        typer.echo(_FRAGILE_COMMAND_WARNING.format(command=sibling), err=True)
        return sibling

    fallback = sibling or which or "nauro"
    typer.echo(_UNRESOLVED_COMMAND_WARNING.format(command=fallback), err=True)
    return fallback


@functools.cache
def _find_nauro_codex_hook_command() -> str | None:
    command = _find_nauro_command()
    if probe_nauro_command(command, args=_CODEX_HOOK_PROBE_ARGS):
        return command

    sibling = _interpreter_sibling_candidate()
    if (
        sibling is not None
        and sibling != command
        and probe_nauro_command(sibling, args=_CODEX_HOOK_PROBE_ARGS)
    ):
        typer.echo(
            f"WARNING: '{command}' does not support Codex bootstrap hooks. "
            f"Recording the current Nauro install at '{sibling}' instead. "
            "Update the durable Nauro install and re-run 'nauro setup all --with-hooks'.",
            err=True,
        )
        return sibling

    typer.echo(
        "WARNING: no installed Nauro command supports Codex bootstrap hooks. "
        "Codex hook wiring was skipped; update Nauro and re-run "
        "'nauro setup all --with-hooks'.",
        err=True,
    )
    return None
