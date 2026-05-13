"""Drift tests for the canonical Nauro skill bodies and dogfood files.

The canonical body lives at ``packages/nauro/src/nauro/skills/{adopt,session}_body.md``.
``load_adopt_body()`` / ``load_session_body()`` return that body via importlib.resources.
``render_skill(surface, skill_name)`` is the single source of truth for both
materialized files (written into user-global / per-repo surface dirs at
``nauro adopt`` time) and the committed dogfood files at the repo root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.skills import load_adopt_body, load_session_body, render_skill

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_load_adopt_body_returns_canonical_bytes():
    body = load_adopt_body()
    assert body.endswith("\n")
    assert 1000 < len(body) < 25000
    # Anchor on key step markers — catches accidental empty / corrupted body.
    assert "Step 1 — Detect repo root" in body
    assert "## Surface modes" in body
    assert "Step 4 — Read code evidence" in body
    assert "Step 6a — Documented decisions" in body
    assert "Step 6b — Code-evidenced" in body
    assert "was Y considered; what pushed you toward X" in body
    assert "Step 11 — Summary" in body


def test_load_session_body_returns_canonical_bytes():
    body = load_session_body()
    assert body.endswith("\n")
    assert 500 < len(body) < 5000
    assert "call get_context" in body
    assert "call check_decision" in body
    assert "call update_state" in body


# --- render_skill produces frontmatter + body ---


def test_render_skill_claude_code_adopt_frontmatter():
    rendered = render_skill("claude_code", "nauro-adopt")
    assert rendered.startswith("---\nname: nauro-adopt\n")
    assert "description:" in rendered.split("\n---\n", 1)[0]
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_adopt_body()


def test_render_skill_cursor_session_frontmatter():
    rendered = render_skill("cursor", "nauro")
    fm = rendered.split("\n---\n", 1)[0]
    assert "description:" in fm
    assert "alwaysApply: false" in fm
    assert "name:" not in fm
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_session_body()


def test_render_skill_unknown_surface_raises():
    with pytest.raises(ValueError):
        render_skill("emacs", "nauro")


def test_render_skill_unknown_skill_raises():
    with pytest.raises(ValueError):
        render_skill("claude_code", "made-up")


# --- Per-surface dogfood files match render_skill() byte-for-byte ---

DOGFOOD_FILES = [
    # (path_relative_to_repo_root, surface, skill_name)
    (".claude/skills/nauro-adopt/SKILL.md", "claude_code", "nauro-adopt"),
    (".claude/skills/nauro/SKILL.md", "claude_code", "nauro"),
    (".cursor/rules/nauro-adopt.mdc", "cursor", "nauro-adopt"),
    (".cursor/rules/nauro.mdc", "cursor", "nauro"),
    (".agents/skills/nauro-adopt/SKILL.md", "codex", "nauro-adopt"),
    (".agents/skills/nauro/SKILL.md", "codex", "nauro"),
]


@pytest.mark.parametrize("rel_path,surface,skill_name", DOGFOOD_FILES)
def test_dogfood_file_matches_render_skill(rel_path: str, surface: str, skill_name: str):
    file_path = REPO_ROOT / rel_path
    assert file_path.is_file(), f"missing dogfood file: {file_path}"
    actual = file_path.read_text(encoding="utf-8")
    expected = render_skill(surface, skill_name)
    assert actual == expected, (
        f"{rel_path} has drifted from render_skill({surface!r}, {skill_name!r}). "
        "Re-render via `python -c 'from nauro.skills import render_skill; ...'` "
        "or update the canonical body."
    )


def test_docs_adopt_prompt_contains_canonical_body():
    """``docs/adopt-prompt.md`` may have a small intro paragraph; canonical body must be present."""
    docs_path = REPO_ROOT / "docs" / "adopt-prompt.md"
    assert docs_path.is_file(), f"missing docs file: {docs_path}"
    content = docs_path.read_text(encoding="utf-8")
    assert load_adopt_body() in content, (
        "docs/adopt-prompt.md does not contain load_adopt_body() — re-append "
        "or update the intro to keep the canonical body in sync."
    )


# --- Retired phrases must not reappear in skill / docs surfaces ---
#
# Anchored to D124/D129/D130/D131 + PR #38. Scope is narrow on purpose: the two
# canonical skill bodies plus ``docs/adopt-prompt.md``. The six dogfood files are
# chained to the source bodies via ``test_dogfood_file_matches_render_skill``.

RETIRED_PHRASES = [
    ("LLM-based", "D130 removed Tier 3 LLM validation"),
    ("Tier 3", "D130 removed Tier 3 LLM validation"),
    ("nauro extract", "D129 retired the extract command"),
    ("[extraction]", "D129 retired the [extraction] extra"),
    ("Anthropic SDK", "D129 dropped Anthropic SDK as a runtime dep"),
    ("Python 3.11+", "D124 lowered the Python floor to 3.10"),
    (
        "propose_decision(title, rationale, rejected,",
        "D131 made propose_decision operation-aware; rejected/confidence are no longer positional",
    ),
    (
        "bracketed-prompt placeholders in `project.md` / `stack.md` / `state_current.md`",
        "PR #38 removed bracket-prompt scaffolding from state_current.md",
    ),
    (
        "The agent does not read source code, tests, IaC templates, or git history during adopt",
        "D125 v2 reverses the docs-only stance — code is evidence on filesystem-capable surfaces",
    ),
    (
        "Step 5a — Clear decisions",
        "D125 v2 renamed to Step 6a — Documented decisions",
    ),
    (
        "Step 5b — Boundary candidates",
        "D125 v2 split this into Step 6b (code-evidenced) + Step 6c (stack inventory)",
    ),
]


def _load_docs_adopt_prompt() -> str:
    return (REPO_ROOT / "docs" / "adopt-prompt.md").read_text(encoding="utf-8")


SCANNED_SURFACES = [
    ("session_body.md", load_session_body),
    ("adopt_body.md", load_adopt_body),
    ("docs/adopt-prompt.md", _load_docs_adopt_prompt),
]


@pytest.mark.parametrize("surface_name,loader", SCANNED_SURFACES)
@pytest.mark.parametrize("phrase,reason", RETIRED_PHRASES)
def test_skill_surface_has_no_retired_phrases(
    surface_name: str, loader, phrase: str, reason: str
) -> None:
    content = loader()
    assert phrase not in content, f"retired phrase {phrase!r} found in {surface_name}: {reason}"
