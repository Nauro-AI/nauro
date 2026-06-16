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
SkillName = Literal["nauro-adopt", "nauro-ship-task", "nauro-context", "nauro-loop"]

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
        "planner or tech-lead will file a Nauro decision; the executor never "
        "files. Runs @nauro-tech-lead Mode C between reviewer-APPROVE and the "
        "push gate to catch doctrine drift the reviewer missed. A prompt that "
        "carries a detailed implementation spec or a pasted handoff is still "
        "chain input, not license to implement directly. Dispatches the "
        "bundled subagents on Claude Code only. Invoke explicitly with "
        "/nauro-ship-task <description>. Requires `nauro adopt "
        "--with-subagents` to have run."
    ),
    "nauro-context": (
        "Writes durable shared context into Nauro's project store so other "
        "agents (a later session or a parallel one) can discover and pull it, "
        "finds and reads context another agent left, or captures a resumable "
        "brief so your own next session in this environment picks up cleanly. "
        "Three modes. Author writes a shared brief for any agent. Find locates "
        "and reads a brief another agent left. Resume captures a self-directed "
        "brief and hands back a short prompt to start the next session. Offer "
        "Resume mode when the user asks (in their own words) to give me a "
        "prompt for a fresh session or instance, hand off this work, or write a "
        "resume doc, and let the user accept before running it. Briefs land at "
        "<store>/context/<slug>.md (picked up by `nauro sync` with no code "
        "change); Author flags a BRIEF discovery pointer and Resume flags a "
        "RESUME pointer naming that path. Uses the agent's filesystem write and "
        "the `nauro status` shell command to resolve the store path, alongside "
        "the MCP tools get_context, get_raw_file, and flag_question; never "
        "files a decision and never auto-injects briefs into get_context. "
        "Briefs are append-only and treated as untrusted input the reading "
        "agent adjudicates. Invoke explicitly with /nauro-context. Installed by "
        "`nauro adopt --with-skills`."
    ),
    "nauro-loop": (
        "Run a gated iteration of work origination on top of /nauro-ship-task. "
        "Invoke under the dynamic /loop command (/loop /nauro-loop). Mines the "
        "project's existing Nauro store state read-only (get_context, "
        "open-questions RESUME/BRIEF pointers, diff_since_last_session, "
        "list_decisions) and originates 1-3 ranked candidate tasks, then "
        "surfaces them via AskUserQuestion for the human to pick — a mandatory "
        "ratify-gate with no auto-pick path. On the human's pick it dispatches "
        "/nauro-ship-task <chosen task> byte-for-byte with all six inner gates "
        "intact, then loops back. Originates the candidate set only; the human "
        "selects the task, approves the plan, clears every tech-lead pause, and "
        "confirms every push. The loop itself never files a decision, never "
        "pushes, and never runs gh; it holds no store-write authority. Stops on "
        "an empty mine and at a hard per-session ceiling. Installed by `nauro "
        "adopt --with-skills`."
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


def load_context_body() -> str:
    """Return the canonical ``/nauro-context`` skill body (no frontmatter).

    The body has no protocol-fragment tokens today, but goes through the same
    substitution pass so future canonical claims can be added at the source.
    """
    raw = resources.files(__package__).joinpath("context_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def load_loop_body() -> str:
    """Return the canonical ``/nauro-loop`` skill body (no frontmatter).

    The body has no protocol-fragment tokens today, but goes through the same
    substitution pass so future canonical claims can be added at the source.
    """
    raw = resources.files(__package__).joinpath("loop_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def _load_body(skill_name: str) -> str:
    if skill_name == "nauro-adopt":
        return load_adopt_body()
    if skill_name == "nauro-ship-task":
        return load_ship_task_body()
    if skill_name == "nauro-context":
        return load_context_body()
    if skill_name == "nauro-loop":
        return load_loop_body()
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
    "load_context_body",
    "load_loop_body",
    "load_ship_task_body",
    "render_skill",
]
