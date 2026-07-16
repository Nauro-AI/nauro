"""Claude Code UserPromptSubmit hook codec (.claude/settings.json) for the setup surface."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.nauro_command import _find_nauro_command
from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_symlink

# Claude Code reads hooks from project-scope ``<repo>/.claude/settings.json``.
# The advisory UserPromptSubmit hook runs ``nauro hook user-prompt-submit`` on
# each turn; it surfaces related decisions as context and never blocks a turn.
# The MVP hook is BM25-floor only — it does not set ``NAURO_EMBEDDINGS`` — so the
# install incurs no embedding model load. The hook still resolves the embeddings
# flag internally, so the follow-up that re-admits cosine-gated embedding hits
# can flip the backend on without changing the installed command.
#
HOOK_EVENT_NAME = "UserPromptSubmit"
# The subcommand the hook entry runs; the full command is built at install time
# by prefixing the resolved absolute nauro path (see _nauro_hook_entry), so the
# hook fires even when nauro is not on the agent's launch PATH.
HOOK_SUBCOMMAND = "hook user-prompt-submit"
HOOK_TIMEOUT_SECONDS = 10

# Substring that identifies a nauro-authored hook entry on the remove path, so a
# user's own UserPromptSubmit hooks are preserved. Matches the subcommand rather
# than "nauro " so it holds regardless of how the entrypoint resolves — a bare
# "nauro", an absolute POSIX path, or a Windows "nauro.exe".
_HOOK_COMMAND_MARKER = HOOK_SUBCOMMAND


def _claude_settings_path(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def materialize_hooks_claude_code(repo: Path, *, remove: bool) -> str:
    """Add or remove the Nauro advisory hook in ``<repo>/.claude/settings.json``.

    Add path: idempotently append the hook entry to
    ``hooks.UserPromptSubmit[].hooks[]`` only when no nauro-authored entry is
    already present. Remove path: strip only the nauro-authored entry (matched on
    the command containing ``nauro hook``), preserving any user-authored hooks
    and the surrounding structure.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    refusal = find_symlink(repo, ".claude/settings.json")
    if refusal is not None:
        return f"  {repo}: {refusal.message}"
    settings_path = _claude_settings_path(repo)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo}: could not parse .claude/settings.json - {exc}"
        if not isinstance(settings, dict):
            return f"  {repo}: .claude/settings.json is not a JSON object, skipped"
    else:
        settings = {}

    if remove:
        return _remove_hook_entry(settings_path, settings, repo)
    return _add_hook_entry(settings_path, settings, repo)


def _nauro_hook_entry() -> dict:
    return {
        "type": "command",
        "command": f"{_find_nauro_command()} {HOOK_SUBCOMMAND}",
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def _is_nauro_hook(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and _HOOK_COMMAND_MARKER in entry["command"]
    )


def _add_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"  {repo}: hooks key is not a JSON object, skipped"
    event_matchers = hooks.setdefault(HOOK_EVENT_NAME, [])
    if not isinstance(event_matchers, list):
        return f"  {repo}: hooks.{HOOK_EVENT_NAME} is not a JSON array, skipped"

    # Idempotent: if any matcher already carries a nauro hook, do nothing.
    for matcher in event_matchers:
        if isinstance(matcher, dict):
            for entry in matcher.get("hooks", []):
                if _is_nauro_hook(entry):
                    return f"  {repo}: nauro hook already present in .claude/settings.json"

    event_matchers.append({"hooks": [_nauro_hook_entry()]})
    atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    lines = [f"  {repo}: wrote nauro hook to .claude/settings.json"]
    lines.extend(public_surface_git_warnings(repo, ".claude/settings.json"))
    return "\n".join(lines)


def _remove_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return f"  {repo}: no nauro hook to remove"
    event_matchers = hooks.get(HOOK_EVENT_NAME)
    if not isinstance(event_matchers, list):
        return f"  {repo}: no nauro hook to remove"

    removed = False
    surviving_matchers = []
    for matcher in event_matchers:
        if not isinstance(matcher, dict):
            surviving_matchers.append(matcher)
            continue
        entries = matcher.get("hooks", [])
        kept = [e for e in entries if not _is_nauro_hook(e)]
        removed_here = len(entries) - len(kept)
        if removed_here:
            removed = True
        if removed_here == 0:
            surviving_matchers.append(matcher)
        elif kept:
            matcher = {**matcher, "hooks": kept}
            surviving_matchers.append(matcher)
        elif set(matcher) - {"hooks"}:
            surviving_matchers.append({**matcher, "hooks": []})
        # Drop only the installer-owned matcher shell with no user metadata.

    if not removed:
        return f"  {repo}: no nauro hook to remove"

    if surviving_matchers:
        hooks[HOOK_EVENT_NAME] = surviving_matchers
    else:
        hooks.pop(HOOK_EVENT_NAME, None)
    if not hooks:
        settings.pop("hooks", None)

    if settings:
        atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    else:
        settings_path.unlink()
    return f"  {repo}: removed nauro hook from .claude/settings.json"
