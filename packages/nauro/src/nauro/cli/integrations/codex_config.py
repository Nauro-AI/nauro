"""Codex ~/.codex/config.toml codec (style-preserving tomlkit) for the setup surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError as TOMLParseError
from tomlkit.items import InlineTable

from nauro.cli.nauro_command import _find_nauro_command
from nauro.store._atomic import atomic_write_text
from nauro.store.write_safety import find_file_symlink


def _default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


@dataclass(frozen=True)
class _CodexNauroEntry:
    """Typed view of the ``[mcp_servers.nauro]`` keys Nauro owns.

    Only ``command`` and ``args`` belong to Nauro; any other keys the user
    added to the entry (timeouts, env, ...) are never touched. A field is
    None when the underlying key is missing or off-shape, so each owned key
    is compared and rewritten independently: a key whose value already
    matches stays untouched, preserving its formatting and comments.
    """

    command: str | None
    args: list[str] | None


def _parse_codex_nauro_entry(entry: object) -> _CodexNauroEntry:
    """Parse an existing ``mcp_servers.nauro`` value into the typed view."""
    if not isinstance(entry, dict):
        return _CodexNauroEntry(command=None, args=None)
    command = entry.get("command")
    args = entry.get("args")
    parsed_args: list[str] | None = None
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        parsed_args = [str(item) for item in args]
    return _CodexNauroEntry(
        command=str(command) if isinstance(command, str) else None,
        args=parsed_args,
    )


def _configure_codex(
    *,
    remove: bool,
    config_path: Path | None = None,
    clear_user_scope: bool = True,
) -> str:
    """Add or remove the Nauro MCP entry in ``~/.codex/config.toml``.

    The file is hand-maintained user config, so edits go through tomlkit:
    comments, formatting, and user-added keys inside the nauro entry survive,
    and only the ``command``/``args`` keys Nauro owns are rewritten. A
    config.toml that is itself a symlink is refused (a dotfile manager may
    own the real file); a symlinked parent directory works. Writes are
    atomic, preserve permission bits, and are skipped when nothing changes.

    ``clear_user_scope`` gates the remove path: when False, the codex MCP
    entry is preserved because other registered nauro projects still depend
    on it. Defaults to True so direct unit callers and the add path retain
    their previous behavior.
    """
    config_path = config_path or _default_codex_config_path()

    if remove and not clear_user_scope:
        return (
            f"Codex: preserved nauro entry in {config_path} (other nauro projects still registered)"
        )

    refusal = find_file_symlink(config_path)
    if refusal is not None:
        return f"Codex: {refusal.message}"

    original: bytes | None
    if config_path.exists():
        original = config_path.read_bytes()
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except UnicodeDecodeError:
            return f"Codex: could not parse {config_path} - not valid UTF-8"
        except TOMLParseError as exc:
            return f"Codex: could not parse {config_path} - {exc}"
    elif remove:
        return f"Codex: no nauro entry to remove in {config_path}"
    else:
        original = None
        document = tomlkit.document()

    servers = document.get("mcp_servers")
    # A hand-edited config.toml could define mcp_servers as a non-table (e.g. a
    # string); mutating it would raise. Skip with a clear message, not a crash.
    if servers is not None and not isinstance(servers, dict):
        if remove:
            return f"Codex: no nauro entry to remove in {config_path}"
        return f"Codex: mcp_servers in {config_path} is not a table, skipped"

    if remove:
        if servers is None or "nauro" not in servers:
            return f"Codex: no nauro entry to remove in {config_path}"
        # The emptied parent table is deliberately left in place: popping it
        # would rewrite user formatting beyond the entry being removed.
        del servers["nauro"]
        status = f"Codex: removed nauro from {config_path}"
    else:
        desired = _CodexNauroEntry(command=_find_nauro_command(), args=["serve", "--stdio"])
        if servers is None:
            servers = tomlkit.table(is_super_table=False)
            document["mcp_servers"] = servers
        entry = servers.get("nauro")
        current = _parse_codex_nauro_entry(entry)
        if current == desired:
            return f"Codex: nauro already configured in {config_path}"
        if isinstance(entry, dict):
            # Per-key update: a key whose value already matches keeps its
            # formatting and comments (e.g. a multiline args array).
            if current.command != desired.command:
                entry["command"] = desired.command
            if current.args != desired.args:
                entry["args"] = desired.args
        elif isinstance(servers, InlineTable):
            # A block table nested inside an inline parent renders invalid
            # TOML; match the user's inline style instead.
            inline = tomlkit.inline_table()
            inline["command"] = desired.command
            inline["args"] = desired.args
            servers["nauro"] = inline
        else:
            table = tomlkit.table()
            table["command"] = desired.command
            table["args"] = desired.args
            servers["nauro"] = table
            # Appended without a leading blank line: a reparse attributes the
            # separator to the preceding entry, which would strand a stray
            # blank line once a later remove deletes the block.
            table.trivia.indent = ""
        status = f"Codex: wrote nauro to {config_path}"

    rendered = tomlkit.dumps(document)
    if original is not None and rendered.encode("utf-8") == original:
        return status
    # newline="\n" is load-bearing: tomlkit output carries the file's original
    # line endings as literal characters, so translation would corrupt CRLF.
    atomic_write_text(config_path, rendered, newline="\n")
    return status
