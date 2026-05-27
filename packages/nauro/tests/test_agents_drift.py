"""Drift tests for the canonical bundled subagent bodies.

The canonical bodies live at
``packages/nauro/src/nauro/agents/<name>.md``. ``load_agent_body(name)``
returns each body via importlib.resources. ``render_agent(surface, name)``
is the single source of truth for what the materializer writes into the
user's surface directory.

Subagent markdown ships with full Claude Code frontmatter inline, so
``render_agent("claude_code", name)`` returns the body unchanged; there
is no per-surface wrapping. Cursor and Codex are stub surfaces and raise
``NotImplementedError`` until a target shape is defined.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.agents import AGENT_NAMES, load_agent_body, render_agent

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "nauro" / "agents"


AGENT_ANCHORS: dict[str, tuple[str, ...]] = {
    "nauro-planner": (
        "Doctrine triage",
        "GREEN, AMBER, or RED",
        "REFUSE TO DRAFT",
    ),
    "nauro-executor": (
        "Stay in scope",
        "Run `ruff format` and `ruff check`",
        "Local completion — do not push",
    ),
    "nauro-reviewer": (
        "VERDICT: APPROVE | BLOCK | APPROVE WITH NITS",
        "Hard rules (BLOCK if any fail)",
        "Code review — find real bugs",
    ),
    "nauro-tech-lead": (
        "How to run — three modes",
        "Never file without user approval.",
        "Mode A: Direction-setting",
    ),
}


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_load_agent_body_returns_canonical_bytes(name: str) -> None:
    body = load_agent_body(name)
    assert body.endswith("\n")
    assert 1000 < len(body) < 30000
    for anchor in AGENT_ANCHORS[name]:
        assert anchor in body, f"missing anchor {anchor!r} in {name}.md"


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_claude_code_returns_body(name: str) -> None:
    rendered = render_agent("claude_code", name)
    assert rendered == load_agent_body(name)
    assert rendered.startswith(f"---\nname: {name}\n")


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_cursor_raises_not_implemented(name: str) -> None:
    with pytest.raises(NotImplementedError):
        render_agent("cursor", name)


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_codex_raises_not_implemented(name: str) -> None:
    with pytest.raises(NotImplementedError):
        render_agent("codex", name)


def test_render_agent_unknown_surface_raises_value_error() -> None:
    with pytest.raises(ValueError):
        render_agent("emacs", "nauro-planner")


def test_render_agent_unknown_agent_raises_value_error() -> None:
    with pytest.raises(ValueError):
        render_agent("claude_code", "nauro-architect")


def test_load_agent_body_unknown_agent_raises_value_error() -> None:
    with pytest.raises(ValueError):
        load_agent_body("nauro-architect")


def test_agent_names_matches_files_on_disk() -> None:
    """Every shipped ``.md`` is registered, and every registered name has a file.

    Drift guard: catches both "file added on disk but missing from the
    ``AGENT_NAMES`` tuple" and the reverse.
    """
    on_disk = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))
    registered = sorted(AGENT_NAMES)
    assert on_disk == registered


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_all_agent_files_have_required_frontmatter(name: str) -> None:
    """Every shipped agent file parses with the keys Claude Code expects.

    ``name`` must equal the filename minus ``.md``. ``description`` and
    ``model`` are required on every file. ``tools`` is required on agents
    that restrict tool access; ``nauro-executor`` intentionally omits the
    key to inherit all available tools.
    """
    body = load_agent_body(name)
    assert body.startswith("---\n"), f"{name}.md is missing opening frontmatter fence"
    end = body.find("\n---\n", 4)
    assert end > 0, f"{name}.md frontmatter is not terminated"
    frontmatter = body[4:end]

    keys = {
        line.split(":", 1)[0].strip()
        for line in frontmatter.splitlines()
        if line and not line.startswith(" ")
    }
    assert "name" in keys
    assert "description" in keys
    assert "model" in keys

    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            assert line.split(":", 1)[1].strip() == name, (
                f"frontmatter name does not match filename for {name}.md"
            )
            break
