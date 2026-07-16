"""JSON MCP config codec (.mcp.json and .cursor/mcp.json) for the setup surface."""

from __future__ import annotations

import json
from enum import Enum, auto
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.integrations._json_config import write_json_config
from nauro.cli.integrations.outcomes import JsonMcpKind, JsonMcpOutcome
from nauro.cli.nauro_command import _find_nauro_command
from nauro.store.write_safety import find_symlink


class McpConfigDocument(BaseModel):
    """Boundary view of a hand-editable ``.mcp.json`` / ``.cursor/mcp.json``.

    Only the container shape Nauro owns is validated: ``mcpServers`` is optional
    and, when present, must be a JSON object. Its entry *values* stay opaque
    (``dict[str, object]``) so a malformed sibling server the user wrote never
    blocks Nauro from wiring or reading its own entry — Nauro does not own those
    entries and must not reject the file over them. Unknown top-level keys are
    preserved. The document validates shape and reads the entry set; writes go
    back into the raw ``json.loads`` dict so key order and untouched content are
    byte-preserved.
    """

    model_config = ConfigDict(extra="allow")

    # Non-optional: a missing key defaults to an empty map (matching the
    # original ``config.get("mcpServers", {})``), while an explicit JSON null
    # or a scalar is a shape violation the boundary parser routes to a graceful
    # skip rather than letting it crash a raw-dict mutation. Bound to the exact
    # ``mcpServers`` alias only (no populate_by_name), so an unrelated snake_case
    # ``mcp_servers`` key the user wrote stays opaque extra content and is never
    # mistaken for Nauro's map.
    mcp_servers: dict[str, object] = Field(default_factory=dict, alias="mcpServers")


class McpShape(Enum):
    TOP_LEVEL_NOT_OBJECT = auto()
    MCPSERVERS_NOT_OBJECT = auto()


class McpShapeError(ValueError):
    """The config's top level or ``mcpServers`` is off-shape."""

    def __init__(self, shape: McpShape) -> None:
        super().__init__(shape.name)
        self.shape = shape


def _parse_mcp_document(raw: object) -> McpConfigDocument:
    """Validate ``raw`` into an :class:`McpConfigDocument` or raise typed.

    The only shape violation the model surfaces is a present-but-non-object
    ``mcpServers`` (null or scalar); its entry values are opaque, so a malformed
    sibling entry parses cleanly and never routes to the skip.
    """
    if not isinstance(raw, dict):
        raise McpShapeError(McpShape.TOP_LEVEL_NOT_OBJECT)
    try:
        return McpConfigDocument.model_validate(raw)
    except ValidationError as exc:
        raise McpShapeError(McpShape.MCPSERVERS_NOT_OBJECT) from exc


def _configure_json_mcp(
    repo_path: Path,
    *,
    config_rel_path: str,
    label: str,
    remove: bool,
) -> JsonMcpOutcome:
    """Add or remove the Nauro MCP entry in a JSON config file at ``repo_path / config_rel_path``.

    Shared shape behind ``_configure_mcp`` (``.mcp.json``) and
    ``_configure_cursor_for_repo`` (``.cursor/mcp.json``): load → parse →
    mutate ``mcpServers["nauro"]`` → write. Both surfaces use the same key
    name and entry shape, so the only per-surface variation is the relative
    path and the human-readable ``label`` used in status messages.

    Shape validation runs through :class:`McpConfigDocument`, but the write
    mutates the raw ``json.loads`` dict so key order and sibling entries stay
    byte-identical.
    """
    refusal = find_symlink(repo_path, config_rel_path)
    if refusal is not None:
        return JsonMcpOutcome(JsonMcpKind.REFUSED_SYMLINK, repo_path, label, refusal=refusal)
    config_path = repo_path / config_rel_path
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return JsonMcpOutcome(JsonMcpKind.PARSE_ERROR, repo_path, label, detail=str(exc))
    else:
        raw = {}

    # A hand-mangled config can have a non-object top level (e.g. a JSON array)
    # or an mcpServers that isn't an object; mutating it would raise. Skip with a
    # clear message instead of a traceback, mirroring the hook path's guard.
    try:
        document = _parse_mcp_document(raw)
    except McpShapeError as exc:
        if exc.shape is McpShape.TOP_LEVEL_NOT_OBJECT:
            return JsonMcpOutcome(JsonMcpKind.NOT_JSON_OBJECT, repo_path, label)
        if remove:
            return JsonMcpOutcome(JsonMcpKind.NOTHING_TO_REMOVE, repo_path, label)
        return JsonMcpOutcome(JsonMcpKind.MCPSERVERS_NOT_OBJECT, repo_path, label)

    servers = document.mcp_servers
    if remove:
        if "nauro" not in servers:
            return JsonMcpOutcome(JsonMcpKind.NOTHING_TO_REMOVE, repo_path, label)
        raw_servers = raw["mcpServers"]
        del raw_servers["nauro"]
        if not raw_servers:
            raw.pop("mcpServers", None)
        if raw:
            write_json_config(config_path, raw)
        else:
            config_path.unlink()
        return JsonMcpOutcome(JsonMcpKind.REMOVED, repo_path, label)

    # The parse guarantees mcpServers is absent or an object here, so setdefault
    # always lands on a dict. Overwrite only Nauro's own entry; every sibling
    # entry stays byte-identical because Nauro does not own it.
    raw.setdefault("mcpServers", {})["nauro"] = nauro_entry
    write_json_config(config_path, raw)
    git_warnings = tuple(public_surface_git_warnings(repo_path, config_rel_path))
    return JsonMcpOutcome(JsonMcpKind.WROTE, repo_path, label, git_warnings=git_warnings)


def _configure_mcp(repo_path: Path, *, remove: bool = False) -> JsonMcpOutcome:
    """Add or remove the Nauro MCP entry in the repo's project-scope ``.mcp.json``.

    Writes the file directly. Mirrors how ``_configure_cursor_for_repo``
    handles ``.cursor/mcp.json`` and ``_configure_codex`` handles
    ``~/.codex/config.toml``, so all three surface handlers share one shape.
    """
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".mcp.json",
        label=".mcp.json",
        remove=remove,
    )


def _configure_cursor_for_repo(repo_path: Path, *, remove: bool) -> JsonMcpOutcome:
    """Add or remove the Nauro MCP entry in this repo's ``.cursor/mcp.json``."""
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".cursor/mcp.json",
        label=".cursor/mcp.json",
        remove=remove,
    )


def recorded_mcp_commands(repo: Path) -> list[str | None]:
    """Recorded nauro MCP commands in this repo's configs, one entry per wired config.

    Single read of ``.mcp.json`` and ``.cursor/mcp.json`` each — presence
    ("the repo is wired" iff the list is non-empty) and the recorded command
    both derive from the same parse via :class:`McpConfigDocument`. A wired
    config whose nauro entry carries a missing, empty, or non-string command
    contributes ``None``: it still counts as wired, but there is nothing to
    probe. Read-only and soft-failing: a missing, unreadable, or off-container
    config contributes nothing — status must never crash on someone else's
    config. Only the entry's ``command`` is read: a malformed sibling entry, or
    a malformed ``args`` on the nauro entry itself, never suppresses a usable
    command, since Nauro does not act on that content here.
    """
    commands: list[str | None] = []
    for rel in (".mcp.json", ".cursor/mcp.json"):
        try:
            raw = json.loads((repo / rel).read_text(encoding="utf-8"))
            document = _parse_mcp_document(raw)
        except Exception:
            continue
        servers = document.mcp_servers
        if "nauro" not in servers:
            continue
        entry = servers["nauro"]
        command = entry.get("command") if isinstance(entry, dict) else None
        commands.append(command if isinstance(command, str) and command else None)
    return commands
