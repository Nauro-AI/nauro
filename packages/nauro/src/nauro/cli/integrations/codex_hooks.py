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
from nauro.cli.git_hygiene import (
    ensure_wiring_ignored,
    public_surface_git_warnings,
    remove_wiring_ignore_entry,
    wiring_path_is_tracked,
)
from nauro.cli.integrations.outcomes import CodexHookKind, CodexHookOutcome
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


def materialize_hooks_codex(repo: Path, *, remove: bool) -> CodexHookOutcome:
    """Add or remove project-scoped Codex lifecycle hooks for ``repo``."""
    refusal = find_symlink(repo, ".codex/hooks.json")
    if refusal is not None:
        return CodexHookOutcome(CodexHookKind.REFUSED_SYMLINK, repo, refusal=refusal)
    # Never write a machine-local absolute path into a git-tracked file; see
    # the JSON MCP codec for the rationale. Teardown stays allowed.
    if not remove and wiring_path_is_tracked(repo, ".codex/hooks.json"):
        return CodexHookOutcome(CodexHookKind.REFUSED_TRACKED, repo)
    hooks_path = _codex_hooks_path(repo)
    existing_text: str | None = None
    if hooks_path.exists():
        try:
            existing_text = hooks_path.read_text(encoding="utf-8")
            config = _parse_codex_hooks(existing_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return CodexHookOutcome(CodexHookKind.PARSE_ERROR, repo, detail=str(exc))
        except _CodexHookConfigError as exc:
            return CodexHookOutcome(CodexHookKind.CONFIG_ERROR, repo, detail=str(exc))
    else:
        config = {}

    try:
        _validate_codex_hooks(config)
    except _CodexHookConfigError as exc:
        return CodexHookOutcome(CodexHookKind.CONFIG_ERROR, repo, detail=str(exc))

    command = None if remove else _find_nauro_codex_hook_command()
    if not remove and command is None:
        return CodexHookOutcome(CodexHookKind.NO_COMMAND, repo)

    try:
        transformed = _transform_codex_hooks(config, command=command)
    except _CodexHookConfigError as exc:
        return CodexHookOutcome(CodexHookKind.CONFIG_ERROR, repo, detail=str(exc))

    if remove:
        if transformed.removed == 0:
            return CodexHookOutcome(
                CodexHookKind.NOTHING_TO_REMOVE,
                repo,
                gitignore=remove_wiring_ignore_entry(repo, ".codex/hooks.json"),
            )
        if transformed.config:
            atomic_write_text(hooks_path, _format_codex_hooks(transformed.config))
        else:
            hooks_path.unlink()
        return CodexHookOutcome(
            CodexHookKind.REMOVED,
            repo,
            gitignore=remove_wiring_ignore_entry(repo, ".codex/hooks.json"),
        )

    rendered = _format_codex_hooks(transformed.config)
    if existing_text == rendered:
        # A repo wired by an older release can be byte-identical yet still
        # unignored; the ignore entry must not depend on a content change.
        return CodexHookOutcome(
            CodexHookKind.ALREADY_PRESENT,
            repo,
            gitignore=ensure_wiring_ignored(repo, ".codex/hooks.json"),
        )
    atomic_write_text(hooks_path, rendered)
    ignore_result = ensure_wiring_ignored(repo, ".codex/hooks.json")
    git_warnings = tuple(public_surface_git_warnings(repo, ".codex/hooks.json"))
    return CodexHookOutcome(
        CodexHookKind.WROTE,
        repo,
        git_warnings=git_warnings,
        gitignore=ignore_result,
    )
