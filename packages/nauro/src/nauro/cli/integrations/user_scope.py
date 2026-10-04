"""User-scope clear policy for the setup surface."""

from __future__ import annotations

import json

from nauro.store.home import registry_file
from nauro.store.local_files import UnreadableFileError, read_text_or_absent


def _registered_project_keys() -> set[str] | None:
    """Read strict setup evidence; None grants no shared deletion authority."""
    try:
        text = read_text_or_absent(registry_file())
        if text is None:
            return set()
        raw = json.loads(text)
    except (UnreadableFileError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        return None
    projects = raw.get("projects")
    if not isinstance(projects, dict) or any(
        not isinstance(value, dict) for value in projects.values()
    ):
        return None
    return set(projects)


def _user_scope_safe_to_clear(current_project_key: str | None) -> bool:
    """Permit shared cleanup only when known evidence excludes other projects."""
    keys = _registered_project_keys()
    return keys is not None and not (keys - {current_project_key})
