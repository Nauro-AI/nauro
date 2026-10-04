"""User-global Cursor wiring and owned repository migration."""

from __future__ import annotations

import json
from pathlib import Path

from nauro.cli import nauro_command
from nauro.cli.integrations._json_config import write_json_config
from nauro.cli.integrations.json_mcp import McpShapeError, _parse_mcp_document
from nauro.setup.git_hygiene import remove_wiring_ignore_entry
from nauro.setup.outcomes import JsonMcpKind, JsonMcpOutcome, WriteFailure
from nauro.store.local_files import UnreadableFileError, read_text_or_absent
from nauro.store.write_safety import find_file_symlink, find_symlink


def _owned(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    return (
        set(entry) <= {"type", "command", "args"}
        and entry.get("type", "stdio") == "stdio"
        and entry.get("args") == ["serve", "--stdio"]
        and isinstance(command, str)
        and nauro_command.is_nauro_entrypoint(command)
    )


def configure_cursor(*, remove: bool, repo: Path | None = None) -> JsonMcpOutcome:
    """Write global wiring or remove an owned legacy entry without changing siblings."""
    root = repo if repo is not None else Path.home()
    path = root / ".cursor/mcp.json"
    label = str(path) if repo is None else ".cursor/mcp.json"
    try:
        return _edit_cursor(root, path, label, remove=remove, legacy=repo is not None)
    except (UnreadableFileError, json.JSONDecodeError, McpShapeError, RecursionError) as exc:
        return JsonMcpOutcome(JsonMcpKind.PARSE_ERROR, root, label, detail=str(exc))
    except OSError as exc:
        return JsonMcpOutcome(
            JsonMcpKind.WRITE_FAILED, root, label, write_failure=WriteFailure.of(path, exc)
        )


def _edit_cursor(
    root: Path, path: Path, label: str, *, remove: bool, legacy: bool
) -> JsonMcpOutcome:
    refusal = find_symlink(root, label) if legacy else find_file_symlink(path)
    if refusal is not None:
        return JsonMcpOutcome(JsonMcpKind.REFUSED_SYMLINK, root, label, refusal=refusal)
    text = read_text_or_absent(path)
    raw = json.loads(text) if text is not None else {}
    document = _parse_mcp_document(raw)
    if remove:
        return _remove_cursor(root, path, label, raw, document.mcp_servers, legacy=legacy)
    command = nauro_command._find_nauro_command()
    if not (
        Path(command).is_absolute()
        and nauro_command.is_nauro_entrypoint(command)
        and nauro_command._is_durable_install_path(Path(command))
        and nauro_command.probe_nauro_command(command)
    ):
        return JsonMcpOutcome(
            JsonMcpKind.PRESERVED,
            root,
            label,
            detail="no working durable absolute Nauro executable",
        )
    desired = {"type": "stdio", "command": command, "args": ["serve", "--stdio"]}
    if document.mcp_servers.get("nauro") == desired:
        return JsonMcpOutcome(JsonMcpKind.UNCHANGED, root, label)
    raw.setdefault("mcpServers", {})["nauro"] = desired
    write_json_config(path, raw)
    return JsonMcpOutcome(JsonMcpKind.WROTE, root, label)


def _remove_cursor(
    root: Path, path: Path, label: str, raw: dict, servers: dict[str, object], *, legacy: bool
) -> JsonMcpOutcome:
    entry = servers.get("nauro")
    if "nauro" not in servers:
        return JsonMcpOutcome(
            JsonMcpKind.NOTHING_TO_REMOVE,
            root,
            label,
            gitignore=remove_wiring_ignore_entry(root, label)
            if legacy and not path.exists()
            else None,
        )
    if not _owned(entry):
        return JsonMcpOutcome(
            JsonMcpKind.PRESERVED, root, label, detail="entry ownership is uncertain"
        )
    del raw["mcpServers"]["nauro"]
    if not raw["mcpServers"]:
        del raw["mcpServers"]
    if raw:
        write_json_config(path, raw)
    else:
        path.unlink()
    ignore = remove_wiring_ignore_entry(root, label) if legacy and not raw else None
    return JsonMcpOutcome(JsonMcpKind.REMOVED, root, label, gitignore=ignore)
