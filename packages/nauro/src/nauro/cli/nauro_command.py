"""Resolve and validate the durable nauro command for recorded MCP/hook wiring.

Wiring files arrive with a clone, so a command read back from one is evidence of wiring,
never an instruction: status executes a recorded command only when ``is_probe_safe`` says
it is Nauro's own entrypoint and not a file the repo itself ships.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import typer

from nauro.cli._codex_hooks import _CODEX_HOOK_PROBE_ARGS
from nauro.cli.git_hygiene import wiring_path_is_tracked
from nauro.store.write_safety import find_symlink

_ENTRYPOINT_NAMES = frozenset({"nauro", "nauro.exe"})


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


def is_nauro_entrypoint(command: str) -> bool:
    """True when ``command`` is bare ``nauro`` or an absolute path to a ``nauro`` console script.
    A relative path or any other program is never Nauro's own entrypoint.
    """
    if command.lower() in _ENTRYPOINT_NAMES:
        return True
    path = Path(command)
    return path.is_absolute() and path.name.lower() in _ENTRYPOINT_NAMES


def is_probe_safe(command: str, repo_roots: Iterable[Path]) -> bool:
    """True when status may execute a recorded ``command``.
    It must be Nauro's entrypoint and must not be a file that any of ``repo_roots`` ships.
    """
    if not is_nauro_entrypoint(command):
        return False
    target = command if Path(command).is_absolute() else shutil.which(command)
    if target is None:
        return True
    roots = list(repo_roots)
    try:
        candidates = {Path(target), Path(target).resolve()}
    except OSError:
        return False
    return not any(_is_repo_shipped(path, root) for path in candidates for root in roots)


def _is_repo_shipped(target: Path, root: Path) -> bool:
    """True when ``target`` lies inside ``root`` and is git-tracked or reached through a symlink.
    Either shape arrived with the clone, so it is the repo author's program, not a Nauro install.
    """
    try:
        rel = target.relative_to(root)
    except ValueError:
        try:
            rel = target.relative_to(root.resolve())
        except (OSError, ValueError):
            return False
    relative = rel.as_posix()
    return find_symlink(root, relative) is not None or wiring_path_is_tracked(root, relative)


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
