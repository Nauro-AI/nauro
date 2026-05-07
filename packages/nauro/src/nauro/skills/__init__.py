"""Skill body loaders.

The canonical Nauro skill bodies live in this package as ``.md`` files.
Per-surface files (Claude Code SKILL.md, Cursor ``.mdc``, Codex SKILL.md)
wrap the same canonical body in surface-appropriate frontmatter; drift
tests assert the wrapped bodies match ``load_*_body()`` exactly.
"""

from __future__ import annotations

from importlib import resources


def load_adopt_body() -> str:
    """Return the canonical ``/nauro-adopt`` skill body (no frontmatter)."""
    return resources.files(__package__).joinpath("adopt_body.md").read_text(encoding="utf-8")


def load_session_body() -> str:
    """Return the canonical ``/nauro`` session-time skill body (no frontmatter)."""
    return resources.files(__package__).joinpath("session_body.md").read_text(encoding="utf-8")


__all__ = ["load_adopt_body", "load_session_body"]
