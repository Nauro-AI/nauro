"""Pure rendering, inspection, and mutation for Codex hook configuration."""

from __future__ import annotations

import copy
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CODEX_HOOK_EVENTS: tuple[str, ...] = ("SessionStart", "SubagentStart")
_CODEX_HOOK_SUBCOMMAND = "hook codex-bootstrap"
_CODEX_HOOK_PROBE_ARGS: tuple[str, ...] = ("hook", "codex-bootstrap", "--help")
_CODEX_HOOK_TIMEOUT_SECONDS = 10
_CODEX_HOOK_STATUS_MESSAGE = "Loading Nauro project context"


class _CodexHookConfigError(ValueError):
    """A hooks object cannot be transformed without risking user data."""


@dataclass(frozen=True)
class _ParsedNauroHook:
    recorded_command: str | None


@dataclass(frozen=True)
class _CodexHookState:
    present: bool
    complete: bool
    recorded_commands: tuple[str | None, ...]


@dataclass(frozen=True)
class _CodexHookTransform:
    config: dict[str, Any]
    removed: int


def _parse_codex_hooks(text: str) -> dict[str, Any]:
    config = json.loads(text)
    if not isinstance(config, dict):
        raise _CodexHookConfigError(".codex/hooks.json is not a JSON object, skipped")
    return config


def _format_codex_hooks(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2) + "\n"


def _render_nauro_hook(command: str) -> dict[str, Any]:
    posix_command = shlex.quote(command)
    windows_command = _powershell_quote(command)
    if Path(command).is_absolute():
        posix_guard = f"test -x {posix_command}"
        windows_script = (
            f"if (Test-Path -LiteralPath {windows_command} -PathType Leaf) "
            f"{{ & {windows_command} {_CODEX_HOOK_SUBCOMMAND} }}; exit 0"
        )
    else:
        posix_guard = f"command -v {posix_command} >/dev/null 2>&1"
        windows_script = (
            f"if (Get-Command {windows_command} -ErrorAction SilentlyContinue) "
            f"{{ & {windows_command} {_CODEX_HOOK_SUBCOMMAND} }}; exit 0"
        )
    return {
        "type": "command",
        "command": f"{posix_guard} || exit 0; exec {posix_command} {_CODEX_HOOK_SUBCOMMAND}",
        "commandWindows": (
            f'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "{windows_script}"'
        ),
        "timeout": _CODEX_HOOK_TIMEOUT_SECONDS,
        "statusMessage": _CODEX_HOOK_STATUS_MESSAGE,
    }


def _inspect_nauro_hook(entry: object, *, windows: bool) -> _ParsedNauroHook | None:
    command = _command_for_platform(entry, windows=windows)
    if command is None or _CODEX_HOOK_SUBCOMMAND not in command:
        return None
    uses_windows_override = (
        windows and isinstance(entry, dict) and isinstance(entry.get("commandWindows"), str)
    )
    recorded_command = (
        _extract_windows_executable(command)
        if uses_windows_override
        else _extract_posix_executable(command)
    )
    return _ParsedNauroHook(recorded_command)


def _inspect_codex_hooks(config: object, *, windows: bool) -> _CodexHookState:
    if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
        return _CodexHookState(False, False, ())
    hooks = config["hooks"]
    event_hooks = tuple(
        _inspect_event_hooks(hooks.get(event), windows=windows) for event in _CODEX_HOOK_EVENTS
    )
    commands = tuple(
        hook.recorded_command for hooks_for_event in event_hooks for hook in hooks_for_event
    )
    return _CodexHookState(any(event_hooks), all(event_hooks), commands)


def _inspect_event_hooks(event_matchers: object, *, windows: bool) -> tuple[_ParsedNauroHook, ...]:
    if not isinstance(event_matchers, list):
        return ()
    inspections = []
    for matcher in event_matchers:
        for entry in _matcher_entries(matcher):
            inspection = _inspect_nauro_hook(entry, windows=windows)
            if inspection is not None:
                inspections.append(inspection)
    return tuple(inspections)


def _transform_codex_hooks(
    config: dict[str, Any],
    *,
    command: str | None,
) -> _CodexHookTransform:
    _validate_codex_hooks(config)
    transformed = copy.deepcopy(config)
    hooks = transformed.get("hooks")
    if hooks is None:
        if command is None:
            return _CodexHookTransform(transformed, 0)
        hooks = {}
        transformed["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise _CodexHookConfigError("hooks key in .codex/hooks.json is not a JSON object, skipped")

    removed = _strip_nauro_events(hooks)

    if command is None:
        if not hooks:
            transformed.pop("hooks", None)
        return _CodexHookTransform(transformed, removed)

    entry = _render_nauro_hook(command)
    for event in _CODEX_HOOK_EVENTS:
        hooks.setdefault(event, []).append({"hooks": [entry]})
    return _CodexHookTransform(transformed, removed)


def _validate_codex_hooks(config: dict[str, Any]) -> None:
    hooks = config.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise _CodexHookConfigError("hooks key in .codex/hooks.json is not a JSON object, skipped")
    for event in _CODEX_HOOK_EVENTS:
        event_matchers = hooks.get(event)
        if event_matchers is not None and not isinstance(event_matchers, list):
            raise _CodexHookConfigError(f"hooks.{event} is not a JSON array, skipped")


def _strip_nauro_events(hooks: dict[str, Any]) -> int:
    removed = 0
    for event in _CODEX_HOOK_EVENTS:
        event_matchers = hooks.get(event)
        if event_matchers is None:
            continue
        hooks[event], event_removed = _strip_nauro_hooks(event_matchers)
        removed += event_removed
        if not hooks[event]:
            hooks.pop(event)
    return removed


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _command_for_platform(entry: object, *, windows: bool) -> str | None:
    if not isinstance(entry, dict):
        return None
    if windows and isinstance(entry.get("commandWindows"), str):
        return entry["commandWindows"]
    command = entry.get("command")
    return command if isinstance(command, str) and command else None


def _extract_posix_executable(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == "hook" and tokens[index : index + 2] == ["hook", "codex-bootstrap"]:
            return tokens[index - 1] if index > 0 else None
    return None


def _extract_windows_executable(command: str) -> str | None:
    marker = f" {_CODEX_HOOK_SUBCOMMAND}"
    marker_index = command.rfind(marker)
    if marker_index < 0:
        return None
    parsed = _last_windows_token(command[:marker_index])
    if parsed is None:
        return None
    leading, token = parsed
    if leading and not leading.endswith("&"):
        return None
    if leading and len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1].replace("''", "'")
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1].replace('""', '"')
    invalid = ";{}" if leading else '"'
    if token and not any(char.isspace() or char in invalid for char in token):
        return token
    return None


def _last_windows_token(prefix: str) -> tuple[str, str] | None:
    prefix = prefix.rstrip()
    if not prefix:
        return None
    if prefix[-1] in "'\"":
        start = _quoted_token_start(prefix, prefix[-1])
        if start is None:
            return None
    else:
        start = len(prefix) - 1
        while start >= 0 and not prefix[start].isspace():
            start -= 1
        start += 1
    return prefix[:start].rstrip(), prefix[start:]


def _quoted_token_start(value: str, quote: str) -> int | None:
    index = len(value) - 2
    while index >= 0:
        if value[index] != quote:
            index -= 1
            continue
        if index > 0 and value[index - 1] == quote:
            index -= 2
            continue
        return index
    return None


def _matcher_entries(matcher: object) -> list[Any]:
    if not isinstance(matcher, dict):
        return []
    entries = matcher.get("hooks")
    return entries if isinstance(entries, list) else []


def _is_nauro_hook(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(
        isinstance(entry.get(field), str) and _CODEX_HOOK_SUBCOMMAND in entry[field]
        for field in ("command", "commandWindows")
    )


def _strip_nauro_hooks(event_matchers: list[Any]) -> tuple[list[Any], int]:
    surviving_matchers = []
    removed = 0
    for matcher in event_matchers:
        if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
            surviving_matchers.append(matcher)
            continue
        entries = matcher["hooks"]
        kept = [entry for entry in entries if not _is_nauro_hook(entry)]
        removed_here = len(entries) - len(kept)
        removed += removed_here
        if removed_here == 0:
            surviving_matchers.append(matcher)
        elif kept:
            surviving_matchers.append({**matcher, "hooks": kept})
        elif set(matcher) - {"hooks"}:
            surviving_matchers.append({**matcher, "hooks": []})
    return surviving_matchers, removed
