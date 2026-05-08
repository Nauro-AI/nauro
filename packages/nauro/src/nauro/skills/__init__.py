"""Skill body loaders + per-surface renderer.

The canonical Nauro skill bodies live in this package as ``.md`` files.
``load_adopt_body()`` / ``load_session_body()`` return them via importlib.resources.
``render_skill(surface, skill_name)`` renders the body wrapped in
surface-appropriate frontmatter — used both for materializing skill files
into the user's surface directories at ``nauro adopt`` time and for the
committed dogfood files at the repo root that drift tests anchor on.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal

Surface = Literal["claude_code", "cursor", "codex"]
SkillName = Literal["nauro", "nauro-adopt"]

SKILL_DESCRIPTIONS: dict[str, str] = {
    "nauro-adopt": (
        "Seeds Nauro's project store from an existing repo's documentation. "
        "Use after the user has run `nauro adopt` locally — reads README, "
        "manifests, ADRs, and Memory-Bank files, surfaces decision candidates "
        "for triage, and writes them to Nauro via existing MCP write tools."
    ),
    "nauro": (
        "Nauro session-time guidance. Reminds the agent to call get_context "
        "at session start, check_decision before architectural changes, and "
        "update_state after meaningful progress."
    ),
}


def load_adopt_body() -> str:
    """Return the canonical ``/nauro-adopt`` skill body (no frontmatter)."""
    return resources.files(__package__).joinpath("adopt_body.md").read_text(encoding="utf-8")


def load_session_body() -> str:
    """Return the canonical ``/nauro`` session-time skill body (no frontmatter)."""
    return resources.files(__package__).joinpath("session_body.md").read_text(encoding="utf-8")


def _load_body(skill_name: str) -> str:
    if skill_name == "nauro-adopt":
        return load_adopt_body()
    if skill_name == "nauro":
        return load_session_body()
    raise ValueError(f"unknown skill: {skill_name!r}")


def _frontmatter(surface: str, skill_name: str) -> str:
    """Build the YAML frontmatter block (terminated by a blank line)."""
    if skill_name not in SKILL_DESCRIPTIONS:
        raise ValueError(f"unknown skill: {skill_name!r}")
    description = SKILL_DESCRIPTIONS[skill_name]
    if surface == "claude_code" or surface == "codex":
        return f"---\nname: {skill_name}\ndescription: {description}\n---\n\n"
    if surface == "cursor":
        return f"---\ndescription: {description}\nalwaysApply: false\n---\n\n"
    raise ValueError(f"unknown surface: {surface!r}")


def render_skill(surface: str, skill_name: str) -> str:
    """Return the full per-surface skill file content (frontmatter + body).

    This is the single source of truth for both materialized skills and the
    committed dogfood files at the repo root — drift tests assert each
    dogfood file equals ``render_skill(...)`` byte-for-byte.
    """
    return _frontmatter(surface, skill_name) + _load_body(skill_name)


__all__ = [
    "SKILL_DESCRIPTIONS",
    "Surface",
    "SkillName",
    "load_adopt_body",
    "load_session_body",
    "render_skill",
]
