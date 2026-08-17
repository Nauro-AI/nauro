"""User-scope clear policy for the setup surface."""

from __future__ import annotations

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
