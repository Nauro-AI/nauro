"""Typed outcomes the setup codecs return for the render layer to emit.

Each codec reports what it did as a structured value rather than a
pre-formatted status string: a small frozen dataclass carrying the facts a
status line needs, tagged by a per-codec ``Kind`` enum. ``render`` (in
``render.py``) is the single place that turns these back into the exact
lines the setup commands echo, so the wording lives in one layer and the
codecs stay free of presentation and of Typer.

``RawLine`` remains for the orchestrator's own policy text (section headers,
advisory paragraphs, the standalone codex count-phrase) that no codec owns.
``HandlerErrorOutcome`` carries the message an orchestrator ``except`` arm
built at the catch site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from nauro.cli.git_hygiene import GitIgnoreResult
from nauro.store.write_safety import SymlinkRefusal, UserSymlinkRefusal


@dataclass(frozen=True)
class RawLine:
    """A pre-rendered advisory or status string carried verbatim."""

    text: str


class JsonMcpKind(Enum):
    REFUSED_SYMLINK = auto()
    REFUSED_TRACKED = auto()
    PARSE_ERROR = auto()
    NOT_JSON_OBJECT = auto()
    MCPSERVERS_NOT_OBJECT = auto()
    WROTE = auto()
    REMOVED = auto()
    NOTHING_TO_REMOVE = auto()


@dataclass(frozen=True)
class JsonMcpOutcome:
    """Result of wiring the Nauro MCP entry in a repo JSON config."""

    kind: JsonMcpKind
    repo_path: Path
    label: str
    refusal: SymlinkRefusal | None = None
    detail: str | None = None
    git_warnings: tuple[str, ...] = ()
    gitignore: GitIgnoreResult | None = None


class ClaudeHookKind(Enum):
    REFUSED_SYMLINK = auto()
    REFUSED_TRACKED = auto()
    PARSE_ERROR = auto()
    NOT_JSON_OBJECT = auto()
    HOOKS_NOT_OBJECT = auto()
    EVENT_NOT_ARRAY = auto()
    ALREADY_PRESENT = auto()
    WROTE = auto()
    REMOVED = auto()
    NOTHING_TO_REMOVE = auto()


@dataclass(frozen=True)
class ClaudeHookOutcome:
    """Result of wiring the advisory Claude Code UserPromptSubmit hook."""

    kind: ClaudeHookKind
    repo: Path
    refusal: SymlinkRefusal | None = None
    detail: str | None = None
    git_warnings: tuple[str, ...] = ()
    gitignore: GitIgnoreResult | None = None
    # True when this run also stripped a stale Nauro entry from the shared
    # .claude/settings.json (the hook now lives in .claude/settings.local.json).
    legacy_cleaned: bool = False


class ClaudeUserConfigKind(Enum):
    REFUSED_SYMLINK = auto()
    INVALID_UTF8 = auto()
    NOT_JSON_OBJECT = auto()
    PRUNED = auto()


@dataclass(frozen=True)
class ClaudeUserConfigOutcome:
    """Result of the user-scope ``~/.claude.json`` MCP prune."""

    kind: ClaudeUserConfigKind
    refusal: UserSymlinkRefusal | None = None


class LegacyKind(Enum):
    REFUSED_SYMLINK = auto()
    REMOVED_BLOCK = auto()
    REMOVED_DELETED_FILE = auto()


@dataclass(frozen=True)
class LegacyOutcome:
    """Result of removing a legacy Nauro block from CLAUDE.md."""

    kind: LegacyKind
    repo_path: Path
    refusal: SymlinkRefusal | None = None


class CodexConfigKind(Enum):
    PRESERVED_OTHER_PROJECTS = auto()
    REFUSED_SYMLINK = auto()
    PARSE_ERROR_UTF8 = auto()
    PARSE_ERROR_TOML = auto()
    NOTHING_TO_REMOVE = auto()
    MCPSERVERS_NOT_TABLE = auto()
    REMOVED = auto()
    ALREADY_CONFIGURED = auto()
    WROTE = auto()


@dataclass(frozen=True)
class CodexConfigOutcome:
    """Result of wiring the user-global Codex MCP entry."""

    kind: CodexConfigKind
    config_path: Path
    refusal: UserSymlinkRefusal | None = None
    detail: str | None = None


class CodexHookKind(Enum):
    REFUSED_SYMLINK = auto()
    REFUSED_TRACKED = auto()
    PARSE_ERROR = auto()
    CONFIG_ERROR = auto()
    NO_COMMAND = auto()
    ALREADY_PRESENT = auto()
    WROTE = auto()
    REMOVED = auto()
    NOTHING_TO_REMOVE = auto()


@dataclass(frozen=True)
class CodexHookOutcome:
    """Result of wiring project-scoped Codex lifecycle hooks."""

    kind: CodexHookKind
    repo: Path
    refusal: SymlinkRefusal | None = None
    detail: str | None = None
    git_warnings: tuple[str, ...] = ()
    gitignore: GitIgnoreResult | None = None


class SkillKind(Enum):
    REFUSED_SYMLINK = auto()
    PRESERVED = auto()
    WROTE = auto()
    REMOVED = auto()
    ABSENT = auto()


@dataclass(frozen=True)
class SkillOutcome:
    """Result of materializing or removing one skill artifact."""

    kind: SkillKind
    target: Path | None = None
    refusal: SymlinkRefusal | UserSymlinkRefusal | None = None
    repo: Path | None = None
    base_label: str | None = None


class AgentKind(Enum):
    REFUSED_SYMLINK = auto()
    PRESERVED = auto()
    PRESERVED_MODIFIED = auto()
    SURFACE_NOT_IMPLEMENTED = auto()
    SURFACE_INVALID = auto()
    UNCHANGED = auto()
    OVERWROTE = auto()
    UPDATED = auto()
    INSTALLED = auto()
    ABSENT = auto()
    REMOVED = auto()


@dataclass(frozen=True)
class AgentOutcome:
    """Result of installing or removing one bundled subagent file."""

    kind: AgentKind
    target: Path | None = None
    refusal: UserSymlinkRefusal | None = None
    surface: str | None = None
    detail: str | None = None
    backup_name: str | None = None


@dataclass(frozen=True)
class HandlerErrorOutcome:
    """A caught per-handler failure the orchestrator reports as one line."""

    message: str


ArtifactOutcome = (
    RawLine
    | JsonMcpOutcome
    | ClaudeHookOutcome
    | ClaudeUserConfigOutcome
    | LegacyOutcome
    | CodexConfigOutcome
    | CodexHookOutcome
    | SkillOutcome
    | AgentOutcome
    | HandlerErrorOutcome
)
