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

    The single subprocess seam for validating a recorded MCP/hook command: the
    setup resolver calls it before recording a command, and ``nauro status``
    calls it to probe wired commands for liveness. A launch failure (missing
    binary or permission error), a hang past ``timeout``, or a non-zero exit
    all count as "won't run". Soft-fails and never raises, so callers can treat
    the boolean as authoritative. Centralized here so tests mock exactly one
    function and no test ever spawns a real binary.
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

    Separator-agnostic via ``Path.parts`` so Windows ``Scripts\\nauro.exe``
    layouts read the same as POSIX ``bin/nauro``. pipx (``.../pipx/venvs/...``)
    and uv-tool (``.../uv/tools/...``) installs live outside any single repo and
    survive that repo's virtualenv being rebuilt or corrupted, so they count as
    durable. A path whose grandparent directory is a bare ``.venv``/``venv``/
    ``env`` is a project-local virtualenv that dies with the checkout, so it
    counts as fragile. Any other shape (system, Homebrew, conda) is treated as
    durable. This is only a resolver tiebreaker — a fragile path that still runs
    is recorded with a warning, never dropped.
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
    """Return the absolute path to a ``nauro`` console script next to the running
    interpreter, or None when there isn't one.

    This is the install the user actually invoked, which pipx/uv-tool layouts
    keep off the PATH that GUI-launched agents see — recording its absolute path
    is what makes the spawned stdio server and per-turn hook independent of the
    agent's launch environment.
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
    """Resolve — and cache for the process — the nauro entrypoint recorded into
    MCP and hook configs.

    Cached so `setup all` validates the entrypoint once rather than once per
    sink (five subprocess probes collapse to one). Warnings surface on the
    cache-miss resolution only; tests reset via
    ``_find_nauro_command.cache_clear()``.
    """
    return _resolve_nauro_command()


def _resolve_nauro_command() -> str:
    """Pick the nauro entrypoint to record into MCP/hook configs.

    Prefers a validated, durable install so the recorded command keeps working
    after a project virtualenv is rebuilt, moved, or corrupted (the observed
    failure: a ``uv run`` / ``.venv``-invoked setup recorded a fragile
    repo-venv path that later died). Resolution order:

      1. Interpreter-sibling that both runs and looks durable — the fast path,
         byte-identical to the historical behavior for pipx/uv-tool/desktop.
      2. Otherwise a PATH-resolved absolute shim that runs and looks durable —
         diverts away from a dead or fragile project venv.
      3. Otherwise the sibling if it merely runs (fragile but working) —
         recorded with a loud warning naming the project-venv fragility.
      4. Otherwise the best absolute path we have (else bare ``nauro``), with a
         loud warning that MCP will not work until nauro is on a durable PATH.

    An absolute path is always preferred over bare ``nauro``; bare ``nauro`` is
    only the terminal fallback, because GUI-launched agents start with an empty
    PATH. Durability checks run before the (subprocess) probe so a non-durable
    candidate short-circuits without spawning.
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
