"""Bundled workflow subagent bodies + per-surface renderer.

The ``.md`` files in this package are the canonical bodies for Nauro's
workflow subagents (``@nauro-planner``, ``@nauro-executor``,
``@nauro-reviewer``, ``@nauro-tech-lead``). Each file ships with full
Claude Code subagent frontmatter (``name``, ``description``, optional
``tools``, ``model``) so the surface renderer can return the body
unchanged on the Claude Code surface — no per-surface frontmatter
wrapping is needed.

``render_agent(surface, name)`` is the single source of truth for what
the materializer writes into ``~/.claude/agents/<name>.md``. Cursor and
Codex stubs exist so opt-in install across surfaces is wired symmetrically;
they raise ``NotImplementedError`` because their subagent equivalents
(if any) are not yet shipped.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal

Surface = Literal["claude_code", "cursor", "codex"]

AGENT_NAMES: tuple[str, ...] = (
    "nauro-planner",
    "nauro-executor",
    "nauro-reviewer",
    "nauro-tech-lead",
)


def load_agent_body(name: str) -> str:
    """Return the canonical bundled body for an agent (frontmatter + prose).

    Unlike ``nauro.skills.load_adopt_body``, the body returned here is the
    full file content including YAML frontmatter — Claude Code subagents
    expect ``name``/``description``/``tools``/``model`` inline at the top
    of the materialized file, so no per-surface wrapping is added.
    """
    if name not in AGENT_NAMES:
        raise ValueError(f"unknown agent: {name!r}")
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")


def render_agent(surface: str, name: str) -> str:
    """Return the per-surface file content for an agent.

    Claude Code returns the body verbatim. Cursor and Codex are stub
    surfaces — the markdown shape needed there is not yet defined, so
    callers receive ``NotImplementedError`` and the install path skips
    that surface rather than crashing.
    """
    if surface == "claude_code":
        return load_agent_body(name)
    if surface in ("cursor", "codex"):
        raise NotImplementedError(f"surface {surface!r} not yet implemented for subagents")
    raise ValueError(f"unknown surface: {surface!r}")


def emit_plugin_agents(dest: Path) -> list[Path]:
    """Render the bundled Claude Code subagents into ``dest/agents/``.

    Writes ``dest/agents/<name>.md`` for every name in ``AGENT_NAMES``,
    using the same ``render_agent("claude_code", name)`` that the installer
    materializes into the user's surface directory. This is the single
    canonical source the cross-repo byte-identity gate verifies against:
    a separate plugin repo renders from here and byte-compares its committed
    copies, so there is no second render path or plugin-specific frontmatter.

    Only the ``agents/`` subtree is created. Returns the written paths.
    """
    agents_dir = dest / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in AGENT_NAMES:
        target = agents_dir / f"{name}.md"
        target.write_text(render_agent("claude_code", name), encoding="utf-8")
        written.append(target)
    return written


__all__ = [
    "AGENT_NAMES",
    "Surface",
    "emit_plugin_agents",
    "load_agent_body",
    "render_agent",
]
