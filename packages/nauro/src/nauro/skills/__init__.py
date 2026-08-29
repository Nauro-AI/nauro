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
SkillName = Literal[
    "nauro-adopt",
    "nauro-ship-task",
    "nauro-context",
    "nauro-loop",
    "nauro-interview",
]

_SHIP_TASK_PREREQUISITES_TOKEN = "<!-- surface:SHIP_TASK_PREREQUISITES -->"
_LOOP_PROGRAM_DELIVERY_LIFECYCLE_TOKEN = "<!-- surface:LOOP_PROGRAM_DELIVERY_LIFECYCLE -->"

_CLAUDE_SHIP_TASK_PREREQUISITES = (
    "This skill invokes the bundled `@nauro-*` subagents by name. They install via "
    "`nauro adopt --with-subagents` (or `nauro setup all --with-subagents`) and "
    "dispatch on Claude Code. If they are missing, or the current surface cannot "
    "spawn subagents, the chain cannot run; surface that to the user and stop. Do "
    "not reproduce the chain inline in the main session: its independent role contexts "
    "and authority gates are required. The personal-subagent path (`@planner` / "
    "`@executor` / `@reviewer` "
    "without the `nauro-` prefix) is not a substitute either. The bundled subagents "
    "call Nauro's MCP tools by design, which is what makes the doctrine gates "
    "load-bearing. On the Claude-rendered agent surface, declared `tools:` allowlists omit "
    "direct Nauro write tools as defense in depth. General shell access retains an indirect "
    "Nauro write path. The explicit no-write instruction and Delivery-parent authority "
    "contract are the portable controls; this does not provide structural capability "
    "denial. Stronger denial belongs to the surface runtime."
)

_CURSOR_SHIP_TASK_PREREQUISITES = (
    "This skill invokes the native Cursor custom agents `/nauro-planner`, "
    "`/nauro-executor`, `/nauro-reviewer`, and `/nauro-tech-lead`. They install under "
    "`.cursor/agents/` in every registered repo via `nauro adopt --with-subagents` "
    "or `nauro setup all --with-subagents`.\n\n"
    "### Cursor dispatch capability check\n\n"
    "Before planning or changing files:\n\n"
    "1. Verify that all four `.cursor/agents/nauro-*.md` files exist in the current "
    "repo.\n"
    "2. Verify that the Cursor runtime loaded the native custom-agent definitions and "
    "can dispatch each configured name. A generic Task agent or prompt mention does not "
    "qualify.\n"
    "3. If any definition or native dispatch capability is missing, explain that the "
    "chain is unavailable and stop before planning, mutation, project-truth writes, "
    "commit, push, or PR creation. Do not reproduce the roles inline and do not use a "
    "generic-agent fallback.\n\n"
    "Cursor custom agents inherit the parent session's MCP tools. The `readonly: true` "
    "field on planner, reviewer, and tech-lead agents does not deny MCP write tools or "
    "every indirect shell path. The explicit draft-only instruction and Delivery-parent "
    "authority contract remain the portable controls. Subagents must not call Nauro write "
    "tools directly or indirectly. Keep every role in a separate context."
)

_CURSOR_SHIP_TASK_DESCRIPTION = (
    "Run the full planner -> executor -> reviewer -> tech-lead -> direct-user-confirm -> "
    "push chain through Cursor's native project workflow agents. Requires the four bundled "
    "`.cursor/agents/nauro-*.md` definitions and fails closed without native dispatch."
)

_CODEX_SHIP_TASK_PREREQUISITES = (
    "This skill invokes the installed `nauro-planner`, `nauro-executor`, "
    "`nauro-reviewer`, and `nauro-tech-lead` custom agents. They install under "
    "`~/.codex/agents/` via `nauro adopt --with-subagents` (or `nauro setup all "
    "--with-subagents`).\n\n"
    "### Codex dispatch capability check\n\n"
    "Before planning or changing files:\n\n"
    "1. Verify that all four `~/.codex/agents/nauro-*.toml` files exist.\n"
    "2. Inspect the callable subagent dispatcher schema. A `task_name` field labels "
    "a generic task; it does not prove that Codex loaded a same-named TOML definition.\n"
    "3. If the dispatcher exposes `agent_type` or an equivalent custom-agent selector, "
    "invoke each installed agent by its configured `name`.\n"
    "4. If the dispatcher cannot select custom agents, explain that a generic fallback "
    "would enforce the role only through task instructions, not through the TOML "
    "`developer_instructions` and `sandbox_mode` configuration layers. Ask: `Use the "
    "instruction-level Codex fallback for this run?` Do not plan, edit, file a "
    "decision, commit, or push before the user explicitly approves.\n"
    "5. On approval, read each installed TOML, start a separate generic subagent with "
    "no inherited conversation context, and pass that agent's exact "
    "`developer_instructions` together with only the task-local handoff. Never treat a "
    "matching `task_name` as custom-agent dispatch. Keep the planner, executor, "
    "reviewer, and tech-lead in separate contexts.\n"
    "6. Record that the instruction-level fallback was used. Include that fact in the "
    "push-gate summary and final receipt. If the user declines, or any agent definition "
    "is missing, stop before mutation.\n\n"
    "The current Codex renderer does not carry the Claude `tools:` allowlists or emit "
    "`mcp_servers` restrictions. Codex can retain direct Nauro MCP write tools and a general "
    "shell write path. Its draft-only boundary is the explicit no-write instruction and "
    "Delivery-parent authority contract only. This does not provide structural capability "
    "denial. Stronger denial belongs to the surface runtime. Keep this limitation visible "
    "when it can affect the user's choice.\n\n"
    "Do not reproduce the four roles inline in the parent session. The independent "
    "contexts and the human-controlled gates remain mandatory in both dispatch modes."
)

_CLAUDE_LOOP_PROGRAM_DELIVERY_LIFECYCLE = (
    "### Claude Code lifecycle proof\n\n"
    "Prove at runtime that Claude Code can create, identify, inspect, and message a fresh "
    "direct-user task. The normal subagent controls do not qualify, even when they can spawn, "
    "list, or message a retained child. If any operation is unavailable before creation, "
    "return exactly one launch prompt and stop: `/nauro-ship-task <DELIVERY>`. Replace "
    "`<DELIVERY>` with the complete self-contained Delivery prompt.\n\n"
    "When all four operations are available, create once, require one stable returned task "
    "identity, record the stable returned identity as the sole launch identity, inspect that "
    "task to confirm its direct user channel, record it as the active Delivery identity, and "
    "message the exact prompt to that identity. Any post-create uncertainty holds without "
    "another create call."
)

_CODEX_LOOP_PROGRAM_DELIVERY_LIFECYCLE = (
    "### Codex lifecycle proof\n\n"
    "Prove at runtime that Codex can create, identify, inspect, and message a fresh "
    "direct-user task. The normal subagent controls do not qualify, including spawn, list, "
    "follow-up, or send-message controls for generic or custom agents. If any operation is "
    "unavailable before creation, return exactly one launch prompt and stop: "
    "`$nauro-ship-task <DELIVERY>`. Replace `<DELIVERY>` with the complete self-contained "
    "Delivery prompt.\n\n"
    "When all four operations are available, create once, require one stable returned task "
    "identity, record the stable returned identity as the sole launch identity, inspect that "
    "task to confirm its direct user channel, record it as the active Delivery identity, and "
    "message the exact prompt to that identity. Any post-create uncertainty holds without "
    "another create call."
)

_CURSOR_LOOP_PROGRAM_DELIVERY_LIFECYCLE = (
    "### Cursor launch hold\n\n"
    "Delivery cannot start on Cursor in this release. Cursor cannot prove runtime controls "
    "to create, identify, inspect, and message a fresh direct-user task. Do not use a hidden "
    "child or generic agent. Return exactly one launch prompt for the supported Claude Code "
    "surface and stop: `/nauro-ship-task <DELIVERY>`. Replace `<DELIVERY>` with the complete "
    "self-contained Delivery prompt."
)

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
        "direct-user-confirm -> push chain for a non-trivial code change against "
        "Nauro's bundled @nauro-* subagents. Every subagent is draft-only for "
        "project-truth writes; the direct-user Delivery parent files exact approved "
        "decision proposals. Runs @nauro-tech-lead Mode C between reviewer-APPROVE "
        "and the push gate to catch doctrine drift the reviewer missed. A prompt that "
        "carries a detailed implementation spec or a pasted handoff is still "
        "chain input, not license to implement directly. Invoke explicitly "
        "with the surface's nauro-ship-task command. Requires `nauro adopt "
        "--with-subagents` to have run. A program Delivery returns a "
        "standardized program handback after PR creation or a terminal blocker."
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
        "Originate gated Delivery and Interview candidates, or coordinate selected Program "
        "Delivery as FRAME -> CHOOSE -> START -> ADVISE -> VERIFY -> ADVANCE. Human-named "
        "work bypasses candidate selection. Agent-originated work keeps read-only ORIENT, "
        "1-3 candidates, mandatory human selection, reject-all, and no auto-pick path. "
        "Each Program slice uses at most one fresh direct-user Delivery task. Automatic "
        "launch requires surface lifecycle support to create, identify, inspect, and message "
        "that task; otherwise the coordinator returns one exact launch prompt and stops. "
        "Coordinator artifact review is advisory, and integration is verified independently. "
        "Synchronous non-program Delivery stays outside the Program state machine. Interview "
        "stays explicit and non-authoritative. Ordinary outputs create no automatic store "
        "artifacts; scheduled ORIENT retains its existing SELECT checkpoint and pointer writes "
        "as a narrow process-state exception. Installed by `nauro adopt --with-skills`."
    ),
    "nauro-interview": (
        "Ask compact, numbered prerequisite-ready questions to elicit tacit project "
        "reasoning or challenge a proposed choice against Nauro decisions and repository "
        "evidence. Continue until every material branch has a disposition, then classify "
        "the result as shared understanding without granting write authority. Use only "
        "when the user explicitly asks to be interviewed, grilled, stress-tested, or helped "
        "to transfer reasoning into Nauro. Runs in the main agent context with no external "
        "skill or subagent dependency."
    ),
}


def _strip_template_header(text: str) -> str:
    """Return ``text`` without its leading ``<!-- Source template ... -->`` hint.

    Any other leading comment is left in place.
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


def load_ship_task_body(surface: str = "claude_code") -> str:
    """Return the ``/nauro-ship-task`` skill body (no frontmatter) for ``surface``.

    The prerequisites token resolves per surface; an unknown surface raises ``ValueError``.
    """
    raw = resources.files(__package__).joinpath("ship_task_body.md").read_text(encoding="utf-8")
    body = substitute_protocol_fragments(_strip_template_header(raw))
    if surface == "codex":
        prerequisites = _CODEX_SHIP_TASK_PREREQUISITES
    elif surface == "claude_code":
        prerequisites = _CLAUDE_SHIP_TASK_PREREQUISITES
    elif surface == "cursor":
        prerequisites = _CURSOR_SHIP_TASK_PREREQUISITES
    else:
        raise ValueError(f"unknown surface: {surface!r}")
    body = body.replace(_SHIP_TASK_PREREQUISITES_TOKEN, prerequisites)
    if surface == "cursor":
        for name in ("nauro-planner", "nauro-executor", "nauro-reviewer", "nauro-tech-lead"):
            body = body.replace(f"`@{name}`", f"`/{name}`")
    return body


def load_context_body() -> str:
    """Return the ``/nauro-context`` skill body (no frontmatter), protocol fragments resolved."""
    raw = resources.files(__package__).joinpath("context_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def load_loop_body(surface: str = "claude_code") -> str:
    """Return the ``/nauro-loop`` skill body (no frontmatter) for ``surface``."""
    raw = resources.files(__package__).joinpath("loop_body.md").read_text(encoding="utf-8")
    body = substitute_protocol_fragments(_strip_template_header(raw))
    if surface == "claude_code":
        lifecycle = _CLAUDE_LOOP_PROGRAM_DELIVERY_LIFECYCLE
    elif surface == "codex":
        lifecycle = _CODEX_LOOP_PROGRAM_DELIVERY_LIFECYCLE
    elif surface == "cursor":
        lifecycle = _CURSOR_LOOP_PROGRAM_DELIVERY_LIFECYCLE
    else:
        raise ValueError(f"unknown surface: {surface!r}")
    return body.replace(_LOOP_PROGRAM_DELIVERY_LIFECYCLE_TOKEN, lifecycle)


def load_interview_body() -> str:
    """Return the ``nauro-interview`` skill body with protocol fragments resolved."""
    raw = resources.files(__package__).joinpath("interview_body.md").read_text(encoding="utf-8")
    return substitute_protocol_fragments(_strip_template_header(raw))


def _load_body(surface: str, skill_name: str) -> str:
    if skill_name == "nauro-adopt":
        return load_adopt_body()
    if skill_name == "nauro-ship-task":
        return load_ship_task_body(surface)
    if skill_name == "nauro-context":
        return load_context_body()
    if skill_name == "nauro-loop":
        return load_loop_body(surface)
    if skill_name == "nauro-interview":
        return load_interview_body()
    raise ValueError(f"unknown skill: {skill_name!r}")


def _frontmatter(surface: str, skill_name: str) -> str:
    """Build the YAML frontmatter block (terminated by a blank line)."""
    if skill_name not in SKILL_DESCRIPTIONS:
        raise ValueError(f"unknown skill: {skill_name!r}")
    description = SKILL_DESCRIPTIONS[skill_name]
    if surface == "cursor" and skill_name == "nauro-ship-task":
        description = _CURSOR_SHIP_TASK_DESCRIPTION
    if surface in ("claude_code", "codex"):
        return f"---\nname: {skill_name}\ndescription: {description}\n---\n\n"
    if surface == "cursor":
        return f"---\ndescription: {description}\nalwaysApply: false\n---\n\n"
    raise ValueError(f"unknown surface: {surface!r}")


def render_skill(surface: str, skill_name: str) -> str:
    """Return the full per-surface skill file content (frontmatter + body).

    Single render path: drift tests byte-compare the committed dogfood files against it.
    """
    return _frontmatter(surface, skill_name) + _load_body(surface, skill_name)


__all__ = [
    "SKILL_DESCRIPTIONS",
    "SkillName",
    "Surface",
    "load_adopt_body",
    "load_context_body",
    "load_interview_body",
    "load_loop_body",
    "load_ship_task_body",
    "render_skill",
]
