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
SkillName = Literal["nauro-adopt", "nauro-ship-task", "nauro-handoff"]

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
    "nauro-ship-task": (
        "Run the full planner -> executor -> reviewer -> tech-lead -> "
        "user-confirm -> push chain for a non-trivial code change against "
        "Nauro's bundled @nauro-* subagents. Gates on the user whenever the "
        "planner or executor will file a Nauro decision; runs @nauro-tech-lead "
        "Mode C between reviewer-APPROVE and the push gate to catch doctrine "
        "drift the reviewer missed. Invoke explicitly with /nauro-ship-task "
        "<description>. Requires `nauro adopt --with-subagents` to have run."
    ),
    "nauro-handoff": (
        "Captures a session handoff to Nauro's project store so the next agent "
        "session resumes cleanly. Writes a handoff body to "
        "<store>/handoffs/<slug>.md (picked up by `nauro sync` with no code "
        "change) and flags a RESUME pointer question naming that path. Composes "
        "existing MCP tools only (get_context, update_state, flag_question, "
        "get_raw_file, diff_since_last_session); never calls propose_decision "
        "and never dumps the handoff into state_current.md. Invoke explicitly "
        "with /nauro-handoff. Installed by `nauro adopt --with-skills`."
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


def load_ship_task_body() -> str:
    """Return the canonical ``/nauro-ship-task`` skill body (no frontmatter).

    The body has no protocol-fragment tokens today, but goes through the same
    substitution pass so future canonical claims can be added at the source.
    """
    raw = resources.files(__package__).joinpath("ship_task_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def load_handoff_body() -> str:
    """Return the canonical ``/nauro-handoff`` skill body (no frontmatter).

    The body has no protocol-fragment tokens today, but goes through the same
    substitution pass so future canonical claims can be added at the source.
    """
    raw = resources.files(__package__).joinpath("handoff_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def _load_body(skill_name: str) -> str:
    if skill_name == "nauro-adopt":
        return load_adopt_body()
    if skill_name == "nauro-ship-task":
        return load_ship_task_body()
    if skill_name == "nauro-handoff":
        return load_handoff_body()
    raise ValueError(f"unknown skill: {skill_name!r}")


def _frontmatter(surface: str, skill_name: str) -> str:
    """Build the YAML frontmatter block (terminated by a blank line)."""
    if skill_name not in SKILL_DESCRIPTIONS:
        raise ValueError(f"unknown skill: {skill_name!r}")
    description = SKILL_DESCRIPTIONS[skill_name]
    if surface in ("claude_code", "codex"):
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
    "SkillName",
    "Surface",
    "load_adopt_body",
    "load_handoff_body",
    "load_ship_task_body",
    "render_skill",
]
