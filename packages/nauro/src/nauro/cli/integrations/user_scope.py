"""User-scope clear policy for the setup surface."""

from __future__ import annotations

import json

from nauro.store.home import registry_file
from nauro.store.local_files import UnreadableFileError, read_text_or_absent
from nauro.store.registry import RegistrySchemaError, load_registry_v2


def _registered_project_keys() -> set[str]:
    """Return the keys of every project in the registry."""
    try:
        registry = load_registry_v2()
    except RegistrySchemaError:
        return set()
    return set(registry.get("projects", {}).keys())


def _user_scope_safe_to_clear(current_project_key: str | None) -> bool:
    """Return True iff no other nauro projects remain in the registry.
    The user-scope skills and the ``nauro`` entry in ``~/.codex/config.toml`` are shared by every
    registered project, so a per-project teardown must not strip them while others remain.
    """
    keys = _registered_project_keys()
    if current_project_key is not None:
        keys.discard(current_project_key)
    return not keys


def _cursor_safe_to_clear(current_project_key: str | None) -> bool:
    """Preserve shared wiring unless registry evidence excludes other projects."""
    try:
        text = read_text_or_absent(registry_file())
        if text is None:
            return True
        raw = json.loads(text)
    except (UnreadableFileError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        return False
    projects = raw.get("projects")
    if not isinstance(projects, dict) or any(
        not isinstance(value, dict) for value in projects.values()
    ):
        return False
    return not (set(projects) - {current_project_key})
