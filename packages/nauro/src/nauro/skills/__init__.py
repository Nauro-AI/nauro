"""Skill body loaders + per-surface renderer.

The ``.md`` files in this package are **source templates**: they may contain
``<!-- protocol:NAME -->`` tokens for canonical protocol claims owned by
``nauro_core.protocol``. The loaders resolve those tokens on the way out, so
every downstream caller — ``render_skill``, dogfood file regeneration,
``docs/adopt-prompt.md`` distribution — sees fully **rendered surfaces** that
must be token-free.

``render_skill(surface, skill_name)`` wraps the (already-substituted) body in
surface-appropriate frontmatter. It is the single source of truth for both
materializing skill files into the user's surface directories at ``nauro
adopt`` time and for the committed dogfood files at the repo root that drift
tests anchor on.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal

from nauro_core.protocol import substitute_protocol_fragments

Surface = Literal["claude_code", "cursor", "codex"]
SkillName = Literal["nauro", "nauro-adopt"]

SKILL_DESCRIPTIONS: dict[str, str] = {
    "nauro-adopt": (
        "Seeds Nauro's project store from an existing repo. Use after "
        "`nauro adopt` has run locally. On filesystem-capable surfaces, reads "
        "docs (README, manifests, ADRs, Memory-Bank) for rationale and "
        "inspects code, config, tests, lockfiles, and recent git history for "
        "evidence, then surfaces targeted probes that turn evidence into "
        "rationale. On chat surfaces, operates on pasted content against an "
        "already-adopted project."
    ),
    "nauro": (
        "Nauro session-time guidance. Reminds the agent to call get_context "
        "at session start, check_decision before architectural changes, and "
        "update_state after meaningful progress."
    ),
}


def _strip_template_header(text: str) -> str:
    """Drop the leading ``<!-- Source template ... -->`` editor hint, if any.

    The hint marks the file as a source template for engineers opening the
    ``.md`` file directly. It is meaningless once the body is rendered into a
    distribution surface, so it is removed before substitution.
    """
    stripped = text.lstrip()
    if stripped.startswith("<!--"):
        end = stripped.find("-->")
        first_line = stripped[4:end].lstrip() if end >= 0 else ""
        if end >= 0 and first_line.startswith("Source template"):
            return stripped[end + 3 :].lstrip("\n")
    return text


def load_adopt_body() -> str:
    """Return the canonical ``/nauro-adopt`` skill body (no frontmatter).

    Protocol-fragment tokens in the source template are resolved before return.
    """
    raw = resources.files(__package__).joinpath("adopt_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def load_session_body() -> str:
    """Return the canonical ``/nauro`` session-time skill body (no frontmatter).

    Protocol-fragment tokens in the source template are resolved before return.
    """
    raw = resources.files(__package__).joinpath("session_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


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
