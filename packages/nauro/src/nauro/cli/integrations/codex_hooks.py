"""Codex hooks codec (.codex/hooks.json) for the setup surface; renders via cli/_codex_hooks."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.cli._codex_hooks import (
    _CodexHookConfigError,
    _format_codex_hooks,
    _parse_codex_hooks,
    _transform_codex_hooks,
    _validate_codex_hooks,
)
from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.nauro_command import _find_nauro_codex_hook_command
from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_symlink


def _nearest_codex_hooks_repo(start: Path) -> Path | None:
    resolved = start.resolve()
    home = Path.home().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate == home:
            break
        if (candidate / ".codex" / "hooks.json").is_file():
            return candidate
    return None


def _codex_hooks_path(repo: Path) -> Path:
    return repo / ".codex" / "hooks.json"


def materialize_hooks_codex(repo: Path, *, remove: bool) -> str:
    """Add or remove project-scoped Codex lifecycle hooks for ``repo``."""
    refusal = find_symlink(repo, ".codex/hooks.json")
    if refusal is not None:
        return f"  {repo}: {refusal.message}"
    hooks_path = _codex_hooks_path(repo)
    existing_text: str | None = None
    if hooks_path.exists():
        try:
            existing_text = hooks_path.read_text(encoding="utf-8")
            config = _parse_codex_hooks(existing_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo}: could not parse .codex/hooks.json - {exc}"
        except _CodexHookConfigError as exc:
            return f"  {repo}: {exc}"
    else:
        config = {}

    try:
        _validate_codex_hooks(config)
    except _CodexHookConfigError as exc:
        return f"  {repo}: {exc}"

    command = None if remove else _find_nauro_codex_hook_command()
    if not remove and command is None:
        return f"  {repo}: Codex hook wiring skipped; no compatible Nauro command"

    try:
        transformed = _transform_codex_hooks(config, command=command)
    except _CodexHookConfigError as exc:
        return f"  {repo}: {exc}"

    if remove:
        if transformed.removed == 0:
            return f"  {repo}: no nauro Codex hooks to remove"
        if transformed.config:
            atomic_write_text(hooks_path, _format_codex_hooks(transformed.config))
        else:
            hooks_path.unlink()
        return f"  {repo}: removed nauro hooks from .codex/hooks.json"

    rendered = _format_codex_hooks(transformed.config)
    if existing_text == rendered:
        return f"  {repo}: nauro hooks already present in .codex/hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(hooks_path, rendered)
    lines = [f"  {repo}: wrote nauro hooks to .codex/hooks.json"]
    lines.extend(public_surface_git_warnings(repo, ".codex/hooks.json"))
    return "\n".join(lines)
