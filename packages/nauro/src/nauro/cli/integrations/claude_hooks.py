"""Claude Code UserPromptSubmit hook codec (.claude/settings.local.json) for the setup surface."""

from __future__ import annotations

import json
from enum import Enum, auto
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nauro.cli.git_hygiene import (
    ensure_wiring_ignored,
    public_surface_git_warnings,
    remove_wiring_ignore_entry,
    wiring_path_is_tracked,
)
from nauro.cli.integrations._json_config import write_json_config
from nauro.cli.integrations.outcomes import ClaudeHookKind, ClaudeHookOutcome
from nauro.cli.nauro_command import _find_nauro_command
from nauro.store.write_safety import find_symlink

# Claude Code merges hooks across the project settings layers. The Nauro hook
# entry carries a machine-local absolute binary path, so it belongs in the
# personal machine-local layer ``.claude/settings.local.json`` — never in the
# team-shared, conventionally committed ``.claude/settings.json``. The shared
# file is only ever touched to strip a stale Nauro entry that an older install
# left there (and on teardown, for the same cleanup).
#
# The advisory UserPromptSubmit hook runs ``nauro hook user-prompt-submit`` on
# each turn; it surfaces related decisions as context and never blocks a turn.
# The MVP hook is BM25-floor only — it does not set ``NAURO_EMBEDDINGS`` — so the
# install incurs no embedding model load. The hook still resolves the embeddings
# flag internally, so the follow-up that re-admits cosine-gated embedding hits
# can flip the backend on without changing the installed command.
#
SETTINGS_LOCAL_REL = ".claude/settings.local.json"
SETTINGS_SHARED_REL = ".claude/settings.json"

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


class HooksMap(BaseModel):
    """The event-keyed hook map inside a Claude settings file.

    Only ``UserPromptSubmit`` — the single event Nauro installs into — is
    validated, and only far enough to confirm it is a JSON array when present.
    Its entries stay opaque (``list[object]``) and are scanned leniently at use.
    Every sibling event is opaque extra content: Nauro does not own it, so it is
    neither validated nor rewritten. Bound to the exact ``UserPromptSubmit``
    alias only (no populate_by_name), so an unrelated snake_case key is never
    mistaken for the event.

    The field carries a default list rather than allowing ``None``: an absent
    key falls back to the default (nothing to scan), while a *present* key —
    including an explicit JSON ``null`` — is validated against ``list[object]``
    and a non-array value raises, routing to the EVENT_NOT_ARRAY skip. That
    distinction is why the type is not ``| None``: ``None`` would swallow an
    explicit null and let the add path append to it and crash.
    """

    model_config = ConfigDict(extra="allow")

    user_prompt_submit: list[object] = Field(default_factory=list, alias=HOOK_EVENT_NAME)


class ClaudeHookSettings(BaseModel):
    """Boundary view of a Claude settings file.

    Only what Nauro touches is validated: the ``hooks`` container is a JSON
    object (an explicit null or scalar is routed to the HOOKS_NOT_OBJECT skip by
    :func:`_parse_hook_settings` before this model runs), and its
    ``UserPromptSubmit`` value is a JSON array when present. See
    :class:`HooksMap`.
    """

    model_config = ConfigDict(extra="allow")

    hooks: HooksMap = Field(default_factory=HooksMap)


class HookShape(Enum):
    TOP_LEVEL_NOT_OBJECT = auto()
    HOOKS_NOT_OBJECT = auto()
    EVENT_NOT_ARRAY = auto()


class HookShapeError(ValueError):
    """The settings top level, ``hooks``, or an event value is off-shape."""

    def __init__(self, shape: HookShape) -> None:
        super().__init__(shape.name)
        self.shape = shape


def _parse_hook_settings(raw: object) -> ClaudeHookSettings:
    """Validate ``raw`` into :class:`ClaudeHookSettings` or raise typed.

    ``hooks`` being a non-object is reported distinctly from an event value
    that is not an array, matching the two guards on the original add path.
    """
    if not isinstance(raw, dict):
        raise HookShapeError(HookShape.TOP_LEVEL_NOT_OBJECT)
    # A present ``hooks`` that is null or a scalar is HOOKS_NOT_OBJECT, distinct
    # from a valid map whose event value is off-shape (EVENT_NOT_ARRAY below).
    if "hooks" in raw and not isinstance(raw["hooks"], dict):
        raise HookShapeError(HookShape.HOOKS_NOT_OBJECT)
    try:
        return ClaudeHookSettings.model_validate(raw)
    except ValidationError as exc:
        raise HookShapeError(HookShape.EVENT_NOT_ARRAY) from exc


def _settings_local_path(repo: Path) -> Path:
    return repo / ".claude" / "settings.local.json"


def _settings_shared_path(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def materialize_hooks_claude_code(repo: Path, *, remove: bool) -> ClaudeHookOutcome:
    """Add or remove the Nauro advisory hook in ``<repo>/.claude/settings.local.json``.

    Add path: idempotently append the hook entry to
    ``hooks.UserPromptSubmit[].hooks[]`` in the machine-local settings file,
    ensure that file is git-ignored, and strip a stale Nauro entry from the
    shared ``.claude/settings.json`` when an older install left one there
    (best-effort; the cleanup diff removes a dead path the team wants gone).
    Remove path: strip the nauro-authored entry from both settings layers
    (matched on the command containing ``nauro hook``), preserving any
    user-authored hooks and the surrounding structure, and drop the managed
    gitignore entry.
    """
    for rel in (SETTINGS_LOCAL_REL, SETTINGS_SHARED_REL):
        refusal = find_symlink(repo, rel)
        if refusal is not None:
            return ClaudeHookOutcome(ClaudeHookKind.REFUSED_SYMLINK, repo, refusal=refusal)
    settings_path = _settings_local_path(repo)

    if remove:
        return _remove_hook_entries(repo, settings_path)

    # Never write a machine-local absolute path into a git-tracked file; see
    # the JSON MCP codec for the rationale. Teardown stays allowed.
    if wiring_path_is_tracked(repo, SETTINGS_LOCAL_REL):
        return ClaudeHookOutcome(ClaudeHookKind.REFUSED_TRACKED, repo)

    if settings_path.exists():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ClaudeHookOutcome(ClaudeHookKind.PARSE_ERROR, repo, detail=str(exc))
        if not isinstance(raw, dict):
            return ClaudeHookOutcome(ClaudeHookKind.NOT_JSON_OBJECT, repo)
    else:
        raw = {}
    return _add_hook_entry(settings_path, raw, repo)


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


def _has_nauro_hook(settings: ClaudeHookSettings) -> bool:
    """True iff a nauro-authored entry already sits in UserPromptSubmit.

    Non-dict matchers and non-nauro entries are skipped, matching main's lenient
    scan; the same :func:`_is_nauro_hook` predicate identifies the entry on the
    remove path, so add and remove agree on what a nauro hook is.
    """
    for matcher in settings.hooks.user_prompt_submit:
        if not isinstance(matcher, dict):
            continue
        for entry in matcher.get("hooks") or []:
            if _is_nauro_hook(entry):
                return True
    return False


def _add_hook_entry(settings_path: Path, raw: dict, repo: Path) -> ClaudeHookOutcome:
    try:
        settings = _parse_hook_settings(raw)
    except HookShapeError as exc:
        if exc.shape is HookShape.HOOKS_NOT_OBJECT:
            return ClaudeHookOutcome(ClaudeHookKind.HOOKS_NOT_OBJECT, repo)
        return ClaudeHookOutcome(ClaudeHookKind.EVENT_NOT_ARRAY, repo)

    # Idempotent: if any matcher already carries a nauro hook, do nothing
    # beyond the legacy strip and the ignore entry, both of which must not
    # depend on a content change (a repo wired by an older run can be
    # byte-identical yet still unignored).
    if _has_nauro_hook(settings):
        legacy_cleaned = _strip_hook_from_file(_settings_shared_path(repo)) is True
        return ClaudeHookOutcome(
            ClaudeHookKind.ALREADY_PRESENT,
            repo,
            gitignore=ensure_wiring_ignored(repo, SETTINGS_LOCAL_REL),
            legacy_cleaned=legacy_cleaned,
        )

    # The parse guarantees hooks is absent or an object and UserPromptSubmit is
    # absent or an array, so both setdefaults land on the right container type.
    raw.setdefault("hooks", {}).setdefault(HOOK_EVENT_NAME, []).append(
        {"hooks": [_nauro_hook_entry()]}
    )
    write_json_config(settings_path, raw)
    # Strip the stale shared-layer entry only after the replacement is durably
    # on disk, so an interrupted run can never leave the repo with no hook.
    legacy_cleaned = _strip_hook_from_file(_settings_shared_path(repo)) is True
    ignore_result = ensure_wiring_ignored(repo, SETTINGS_LOCAL_REL)
    git_warnings = tuple(public_surface_git_warnings(repo, SETTINGS_LOCAL_REL))
    return ClaudeHookOutcome(
        ClaudeHookKind.WROTE,
        repo,
        git_warnings=git_warnings,
        gitignore=ignore_result,
        legacy_cleaned=legacy_cleaned,
    )


def _remove_hook_entries(repo: Path, settings_path: Path) -> ClaudeHookOutcome:
    """Strip the nauro hook from both settings layers and the managed gitignore."""
    if settings_path.exists():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ClaudeHookOutcome(ClaudeHookKind.PARSE_ERROR, repo, detail=str(exc))
        if not isinstance(raw, dict):
            return ClaudeHookOutcome(ClaudeHookKind.NOT_JSON_OBJECT, repo)
        local_removed = _strip_hook_entries(raw)
        if local_removed:
            if raw:
                write_json_config(settings_path, raw)
            else:
                settings_path.unlink()
    else:
        local_removed = False

    legacy_removed = _strip_hook_from_file(_settings_shared_path(repo)) is True
    ignore_result = remove_wiring_ignore_entry(repo, SETTINGS_LOCAL_REL)

    if not local_removed and not legacy_removed:
        return ClaudeHookOutcome(ClaudeHookKind.NOTHING_TO_REMOVE, repo, gitignore=ignore_result)
    return ClaudeHookOutcome(
        ClaudeHookKind.REMOVED,
        repo,
        gitignore=ignore_result,
        legacy_cleaned=legacy_removed,
    )


def _strip_hook_from_file(settings_path: Path) -> bool | None:
    """Best-effort strip of the nauro hook entry from one settings file.

    Returns True when an entry was removed, False when none was present, and
    None when the file was absent, unreadable, or off-shape — the file is left
    untouched in that case, because Nauro no longer owns writes to the shared
    layer and a legacy cleanup must never turn into a rewrite of it.
    """
    if not settings_path.exists():
        return None
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    if not _strip_hook_entries(raw):
        return False
    # Best-effort covers the write side too: an unwritable shared file must
    # not abort the surface whose own hook already landed, and the atomic
    # write's failure leaves the original bytes intact.
    try:
        if raw:
            write_json_config(settings_path, raw)
        else:
            settings_path.unlink()
    except OSError:
        return None
    return True


def _strip_hook_entries(raw: dict) -> bool:
    """Remove nauro-authored UserPromptSubmit entries from ``raw`` in place.

    Preserves user-authored hooks, matcher metadata, and sibling events; drops
    only the installer-owned matcher shell when nothing else remains in it.
    Returns True when at least one entry was removed.
    """
    hooks = raw.get("hooks")
    if not isinstance(hooks, dict):
        return False
    event_matchers = hooks.get(HOOK_EVENT_NAME)
    if not isinstance(event_matchers, list):
        return False

    removed = False
    surviving_matchers = []
    for matcher in event_matchers:
        if not isinstance(matcher, dict):
            surviving_matchers.append(matcher)
            continue
        entries = matcher.get("hooks", [])
        # An off-shape hooks value (null, scalar, object) is user content the
        # strip does not own: pass the matcher through untouched rather than
        # crashing — this path also runs best-effort against the shared file.
        if not isinstance(entries, list):
            surviving_matchers.append(matcher)
            continue
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
        return False

    if surviving_matchers:
        hooks[HOOK_EVENT_NAME] = surviving_matchers
    else:
        hooks.pop(HOOK_EVENT_NAME, None)
    if not hooks:
        raw.pop("hooks", None)
    return True
