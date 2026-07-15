"""User-scope clear policy for the setup surface."""

from __future__ import annotations

from nauro.store.registry import RegistrySchemaError, load_registry, load_registry_v2


def _registered_project_keys() -> set[str]:
    """Return the keys of every project in the registry (v2, v1 fallback)."""
    try:
        registry = load_registry_v2()
    except RegistrySchemaError:
        registry = load_registry()
    return set(registry.get("projects", {}).keys())


def _user_scope_safe_to_clear(current_project_key: str | None) -> bool:
    """Return True iff no other nauro projects remain in the registry.

    User-scope artifacts (``~/.claude/skills/nauro-adopt``,
    ``~/.agents/skills/nauro-adopt``, and the ``nauro`` entry in
    ``~/.codex/config.toml``) are shared by every registered project on the
    machine, so a per-project teardown must not strip them while other
    projects still depend on them.
    """
    keys = _registered_project_keys()
    if current_project_key is not None:
        keys.discard(current_project_key)
    return not keys
