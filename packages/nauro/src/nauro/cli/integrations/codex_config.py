"""Codex ~/.codex/config.toml codec (style-preserving tomlkit) for the setup surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError as TOMLParseError
from tomlkit.items import InlineTable

from nauro.cli.nauro_command import _find_nauro_command
from nauro.setup.outcomes import CodexConfigKind, CodexConfigOutcome, WriteFailure
from nauro.store._atomic import atomic_write_text
from nauro.store.local_files import UnreadableFileError
from nauro.store.write_safety import find_file_symlink

# Codex gives a server ten seconds to answer initialize. A replica's startup
# refresh spends about two seconds initializing plus up to eight renewing an
# expired credential, and a second process renewing at once waits on the first.
CODEX_STARTUP_TIMEOUT_SEC = 20


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


@dataclass(frozen=True)
class _CodexNauroEntry:
    """Typed view of the ``[mcp_servers.nauro]`` keys Nauro reads: ``command`` and ``args``
    are rewritten to Nauro's values, ``startup_timeout_sec`` is set to Nauro's floor unless
    the entry's startup budget already meets it through either Codex key, and the millisecond
    alias is read but never written. A field is ``None`` when its key is missing or off-shape.
    """

    command: str | None
    args: list[str] | None
    startup_timeout_sec: int | float | None
    startup_timeout_ms: int | float | None

    @property
    def startup_budget(self) -> float | None:
        """The startup budget Codex applies: the seconds key first, else the millisecond alias."""
        if self.startup_timeout_sec is not None:
            return float(self.startup_timeout_sec)
        if self.startup_timeout_ms is not None:
            return self.startup_timeout_ms / 1000
        return None


def _seconds(value: object) -> int | float | None:
    """A TOML number as a plain number; booleans and every other shape read as absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if isinstance(value, int) else float(value)


def _parse_codex_nauro_entry(entry: object) -> _CodexNauroEntry:
    """Parse an existing ``mcp_servers.nauro`` value into the typed view."""
    if not isinstance(entry, dict):
        return _CodexNauroEntry(None, None, None, None)
    command = entry.get("command")
    args = entry.get("args")
    parsed_args: list[str] | None = None
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        parsed_args = [str(item) for item in args]
    return _CodexNauroEntry(
        command=str(command) if isinstance(command, str) else None,
        args=parsed_args,
        startup_timeout_sec=_seconds(entry.get("startup_timeout_sec")),
        startup_timeout_ms=_seconds(entry.get("startup_timeout_ms")),
    )


def _apply_nauro_entry(
    servers: dict,
    entry: object,
    current: _CodexNauroEntry,
    desired: _CodexNauroEntry,
) -> None:
    """Write ``desired`` into the ``mcp_servers.nauro`` value, in place.
    A present table gets a per-key update so a matching key keeps its formatting and comments;
    an inline parent gets an inline child; otherwise a fresh block table is appended.
    """
    if isinstance(entry, dict):
        # Per-key update: a key whose value already matches keeps its
        # formatting and comments (e.g. a multiline args array).
        if current.command != desired.command:
            entry["command"] = desired.command
        if current.args != desired.args:
            entry["args"] = desired.args
        if current.startup_timeout_sec != desired.startup_timeout_sec:
            entry["startup_timeout_sec"] = desired.startup_timeout_sec
    elif isinstance(servers, InlineTable):
        # A block table nested inside an inline parent renders invalid
        # TOML; match the user's inline style instead.
        inline = tomlkit.inline_table()
        inline["command"] = desired.command
        inline["args"] = desired.args
        inline["startup_timeout_sec"] = desired.startup_timeout_sec
        servers["nauro"] = inline
    else:
        table = tomlkit.table()
        table["command"] = desired.command
        table["args"] = desired.args
        table["startup_timeout_sec"] = desired.startup_timeout_sec
        servers["nauro"] = table
        # Appended without a leading blank line: a reparse attributes the
        # separator to the preceding entry, which would strand a stray
        # blank line once a later remove deletes the block.
        table.trivia.indent = ""


def _configure_codex(
    *,
    remove: bool,
    config_path: Path | None = None,
    clear_user_scope: bool = True,
) -> CodexConfigOutcome:
    """Add or remove the Nauro MCP entry in ``~/.codex/config.toml``; a symlinked file is refused.
    Edits go through tomlkit, so comments and user-added keys survive and only ``command`` and
    ``args`` are rewritten. The remove path preserves the entry unless ``clear_user_scope``.
    """
    config_path = config_path or codex_config_path()
    try:
        return _edit_codex_config(config_path, remove=remove, clear_user_scope=clear_user_scope)
    except OSError as exc:
        return CodexConfigOutcome(
            CodexConfigKind.WRITE_FAILED,
            config_path,
            write_failure=WriteFailure.of(config_path, exc),
        )


def _edit_codex_config(
    config_path: Path, *, remove: bool, clear_user_scope: bool
) -> CodexConfigOutcome:
    if remove and not clear_user_scope:
        return CodexConfigOutcome(CodexConfigKind.PRESERVED_OTHER_PROJECTS, config_path)

    refusal = find_file_symlink(config_path)
    if refusal is not None:
        return CodexConfigOutcome(CodexConfigKind.REFUSED_SYMLINK, config_path, refusal=refusal)

    original: bytes | None
    if config_path.exists():
        original = config_path.read_bytes()
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except UnicodeDecodeError:
            return CodexConfigOutcome(CodexConfigKind.PARSE_ERROR_UTF8, config_path)
        except TOMLParseError as exc:
            return CodexConfigOutcome(
                CodexConfigKind.PARSE_ERROR_TOML, config_path, detail=str(exc)
            )
    elif remove:
        return CodexConfigOutcome(CodexConfigKind.NOTHING_TO_REMOVE, config_path)
    else:
        original = None
        document = tomlkit.document()

    servers = document.get("mcp_servers")
    # A hand-edited config.toml could define mcp_servers as a non-table (e.g. a
    # string); mutating it would raise. Skip with a clear message, not a crash.
    if servers is not None and not isinstance(servers, dict):
        if remove:
            return CodexConfigOutcome(CodexConfigKind.NOTHING_TO_REMOVE, config_path)
        return CodexConfigOutcome(CodexConfigKind.MCPSERVERS_NOT_TABLE, config_path)

    if remove:
        if servers is None or "nauro" not in servers:
            return CodexConfigOutcome(CodexConfigKind.NOTHING_TO_REMOVE, config_path)
        # The emptied parent table is deliberately left in place: popping it
        # would rewrite user formatting beyond the entry being removed.
        del servers["nauro"]
        status = CodexConfigOutcome(CodexConfigKind.REMOVED, config_path)
    else:
        if servers is None:
            servers = tomlkit.table(is_super_table=False)
            document["mcp_servers"] = servers
        entry = servers.get("nauro")
        current = _parse_codex_nauro_entry(entry)
        budget_met = (current.startup_budget or 0) >= CODEX_STARTUP_TIMEOUT_SEC
        desired = _CodexNauroEntry(
            command=_find_nauro_command(),
            args=["serve", "--stdio"],
            startup_timeout_sec=(
                current.startup_timeout_sec if budget_met else CODEX_STARTUP_TIMEOUT_SEC
            ),
            startup_timeout_ms=current.startup_timeout_ms,
        )
        if current == desired:
            return CodexConfigOutcome(CodexConfigKind.ALREADY_CONFIGURED, config_path)
        _apply_nauro_entry(servers, entry, current, desired)
        status = CodexConfigOutcome(CodexConfigKind.WROTE, config_path)

    rendered = tomlkit.dumps(document)
    if original is not None and rendered.encode("utf-8") == original:
        return status
    # newline="\n" is load-bearing: tomlkit output carries the file's original
    # line endings as literal characters, so translation would corrupt CRLF.
    atomic_write_text(config_path, rendered, newline="\n")
    return status


def recorded_codex_command() -> tuple[bool, str | None]:
    """Return ``(wired, recorded command)`` for the user-global Codex config.
    ``(True, None)`` means a nauro entry exists but records no usable command. A missing config
    is not wired; one that cannot be read or parsed raises UnreadableFileError.
    """
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    path = codex_config_path()
    try:
        with path.open("rb") as f:
            config = tomllib.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return (False, None)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UnreadableFileError(path, str(exc)) from exc
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or "nauro" not in servers:
        return (False, None)
    entry = servers["nauro"]
    cmd = entry.get("command") if isinstance(entry, dict) else None
    return (True, cmd if isinstance(cmd, str) and cmd else None)
