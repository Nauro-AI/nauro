"""Drift tests for the canonical Nauro skill bodies and dogfood files.

The canonical body lives at ``packages/nauro/src/nauro/skills/adopt_body.md``.
``load_adopt_body()`` returns that body via importlib.resources.
``render_skill(surface, skill_name)`` is the single source of truth for both
materialized files (written into user-global / per-repo surface dirs at
``nauro adopt`` time) and the committed dogfood files at the repo root.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from nauro_core.constants import MAX_BRIEF_BYTES, MAX_DELTA_LENGTH

from nauro.skills import (
    load_adopt_body,
    load_context_body,
    load_loop_body,
    load_ship_task_body,
    render_skill,
)
from tests._skill_surfaces import REPO_ROOT, SKILL_SURFACES, load_docs_adopt_prompt


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
    # All three operation variants must remain present so a cleanup edit
    # cannot accidentally drop one — the structural test only validates
    # calls that *are* there, not that all three exist.
    for op in ("add", "update", "supersede"):
        assert f'operation="{op}"' in body, f"missing propose_decision variant: {op}"
    assert "Step 11 — Summary" in body


def test_load_ship_task_body_returns_canonical_bytes():
    body = load_ship_task_body()
    assert body.endswith("\n")
    assert 1000 < len(body) < 25000
    # Anchor on key chain markers so a cleanup edit cannot accidentally drop
    # a load-bearing step heading.
    assert "## Prerequisites" in body
    assert "## Pre-step" in body
    assert "@nauro-planner" in body
    assert "@nauro-executor" in body
    assert "@nauro-reviewer" in body
    assert "@nauro-tech-lead" in body
    # Nauro-strict gate language must remain — the chain always gates when
    # doctrine writes are pending; there is no low-stakes auto-proceed path.
    assert "propose_decision" in body
    # Tech-lead Mode C pass sits between reviewer-APPROVE and the push gate.
    assert "Mode C" in body
    # Required prerequisite reference to the bundled subagents flag.
    assert "--with-subagents" in body
    # PR creation goes through a body file — an inline body argument breaks
    # on quote characters in the drafted description.
    assert "--body-file" in body


def test_load_context_body_returns_canonical_bytes():
    body = load_context_body()
    # Byte hygiene: exactly one trailing newline (mirrors the handoff guard).
    assert body.endswith("\n")
    assert not body.endswith("\n\n")
    assert 1000 < len(body) < 25000
    # Load-bearing step headings so a cleanup edit cannot silently drop the
    # author -> pointer -> find chain.
    assert "## Step" in body
    # The three MCP tools the skill composes must all be named in the body.
    assert "get_context" in body
    assert "get_raw_file" in body
    assert "flag_question" in body
    # Briefs live under context/ and are discovered via a literal BRIEF: pointer
    # on the union-merged open-questions.md, never a shared index file.
    assert "context/" in body
    assert "BRIEF:" in body
    # Resume mode is the converged third mode: it flags a literal RESUME: pointer
    # and carries its own step section. Anchoring both guards against a cleanup
    # edit silently dropping the resume path (the converged nauro-handoff role).
    assert "RESUME:" in body
    assert "## Step R1 — Resume" in body
    # Regression (dogfood-verified): the discovery surface survives concurrent
    # authors because open-questions.md is set-union-merged on sync — NOT because
    # of a lock (the store lock guards only same-machine local appends). The
    # corrected skill must not reintroduce the wrong "lock-protected" claim, and
    # must teach the agent how to resolve the store path it writes into.
    assert "lock-protected" not in body
    assert "nauro status" in body
    # Like nauro-handoff, the skill runs in the main-agent context with no
    # tool-lock, so it must only DRAFT decisions for the user to file -- it
    # never autonomously commits doctrine. Load-bearing guard for that.
    assert "propose_decision" not in body
    # No leaked template syntax.
    assert "<!--" not in body
    assert "{{" not in body


def test_load_loop_body_returns_canonical_bytes():
    body = load_loop_body()
    # Byte hygiene: exactly one trailing newline (mirrors the context guard).
    assert body.endswith("\n")
    assert not body.endswith("\n\n")
    assert 1000 < len(body) < 25000
    # Load-bearing section headings so a cleanup edit cannot silently drop the
    # ORIENT -> SELECT -> CHAIN -> INTEGRATE -> RE-ORIENT procedure.
    assert "## ORIENT" in body
    assert "## SELECT" in body
    assert "## CHAIN" in body
    assert "## INTEGRATE" in body
    assert "## RE-ORIENT" in body
    # SELECT is the net-new human ratify-gate; it surfaces candidates via
    # AskUserQuestion and never auto-picks — not even a single candidate.
    assert "AskUserQuestion" in body
    assert "no auto-pick" in body
    # The loop holds no decision-FILING authority. The literal write-tool token
    # must stay absent (mirrors the context guard that the loop runs in the
    # main-agent context with no tool-lock); the guard is carried by prose that
    # says the loop never files a decision / holds no write authority.
    assert "propose_decision" not in body
    assert "never files a decision" in body
    assert "no store-write authority" in body or "holds no store-write authority" in body
    # The chain is dispatched byte-for-byte and never reproduced inline.
    assert "/nauro-ship-task" in body
    assert "byte-for-byte" in body
    # Under the loop the chain's low-stakes auto-proceed at the plan gate closes.
    assert "auto-proceed" in body
    assert "closed" in body or "CLOSED" in body
    # Structural hard rules: fail-closed on gate-callback timeout, a held-gate
    # lock, and a hard per-session ceiling.
    assert "fails closed" in body or "fail closed" in body
    assert "held-gate lock" in body or "held gate" in body
    assert "per-session ceiling" in body
    # Gate H is the stuck-handler: a chain that self-halts or fails loud routes
    # to a surface-and-wait gate, never a blind retry or skip to the next task.
    assert "Gate H" in body
    # ORIENT mines via the Resume R1/R2 pointers on the union-merged file.
    assert "RESUME:" in body
    assert "BRIEF:" in body
    # Unattended substrates (cron / scheduled wakeups / routines) are out of
    # scope — they cannot pause for the SELECT sign-off.
    assert "cron" in body
    assert "out of scope" in body
    # No leaked template syntax.
    assert "<!--" not in body
    assert "{{" not in body


def test_render_skill_claude_code_loop_frontmatter():
    rendered = render_skill("claude_code", "nauro-loop")
    assert rendered.startswith("---\nname: nauro-loop\n")
    assert "description:" in rendered.split("\n---\n", 1)[0]
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_loop_body()


def test_render_skill_cursor_loop_frontmatter():
    rendered = render_skill("cursor", "nauro-loop")
    fm = rendered.split("\n---\n", 1)[0]
    assert "description:" in fm
    assert "alwaysApply: false" in fm
    assert "name:" not in fm
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_loop_body()


def test_context_body_brief_size_gloss_matches_constant():
    """The body glosses ``MAX_BRIEF_BYTES`` in prose as a human-readable size.

    The constant is the enforced cap in the sync push; the prose gloss is what
    an agent reads. If the constant changes, the prose must change with it, or
    the skill teaches a cap the code does not enforce. Pin the gloss to the
    constant so the two cannot drift apart silently.
    """
    body = load_context_body()
    gloss = f"{MAX_BRIEF_BYTES // 1024} KiB"
    assert gloss in body, (
        f"context_body.md size gloss is out of sync with MAX_BRIEF_BYTES "
        f"({MAX_BRIEF_BYTES} bytes); expected the prose to read {gloss!r}."
    )


def test_adopt_body_delta_size_gloss_matches_constant():
    """The body glosses ``MAX_DELTA_LENGTH`` in prose as a human-readable count.

    The constant is the enforced cap on ``update_state`` deltas; the prose
    gloss is what an agent reads at the Step 8 write. If the constant changes,
    the prose must change with it, or the skill teaches a cap the code does
    not enforce. Pin the gloss to the constant so the two cannot drift apart
    silently.
    """
    body = load_adopt_body()
    gloss = f"{MAX_DELTA_LENGTH:,} characters"
    assert gloss in body, (
        f"adopt_body.md delta-cap gloss is out of sync with MAX_DELTA_LENGTH "
        f"({MAX_DELTA_LENGTH} characters); expected the prose to read {gloss!r}."
    )


# --- render_skill produces frontmatter + body ---


def test_render_skill_claude_code_adopt_frontmatter():
    rendered = render_skill("claude_code", "nauro-adopt")
    assert rendered.startswith("---\nname: nauro-adopt\n")
    assert "description:" in rendered.split("\n---\n", 1)[0]
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_adopt_body()


def test_render_skill_cursor_adopt_frontmatter():
    rendered = render_skill("cursor", "nauro-adopt")
    fm = rendered.split("\n---\n", 1)[0]
    assert "description:" in fm
    assert "alwaysApply: false" in fm
    assert "name:" not in fm
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_adopt_body()


def test_render_skill_claude_code_ship_task_frontmatter():
    rendered = render_skill("claude_code", "nauro-ship-task")
    assert rendered.startswith("---\nname: nauro-ship-task\n")
    assert "description:" in rendered.split("\n---\n", 1)[0]
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_ship_task_body()


def test_render_skill_cursor_ship_task_frontmatter():
    rendered = render_skill("cursor", "nauro-ship-task")
    fm = rendered.split("\n---\n", 1)[0]
    assert "description:" in fm
    assert "alwaysApply: false" in fm
    assert "name:" not in fm
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_ship_task_body()


def test_render_skill_claude_code_context_frontmatter():
    rendered = render_skill("claude_code", "nauro-context")
    assert rendered.startswith("---\nname: nauro-context\n")
    assert "description:" in rendered.split("\n---\n", 1)[0]
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_context_body()


def test_render_skill_cursor_context_frontmatter():
    rendered = render_skill("cursor", "nauro-context")
    fm = rendered.split("\n---\n", 1)[0]
    assert "description:" in fm
    assert "alwaysApply: false" in fm
    assert "name:" not in fm
    body = rendered.split("\n---\n", 1)[1].lstrip("\n")
    assert body == load_context_body()


def test_render_skill_unknown_surface_raises():
    with pytest.raises(ValueError):
        render_skill("emacs", "nauro-adopt")


def test_render_skill_unknown_skill_raises():
    with pytest.raises(ValueError):
        render_skill("claude_code", "made-up")


# --- Per-surface dogfood files match render_skill() byte-for-byte ---

DOGFOOD_FILES = [
    # (path_relative_to_repo_root, surface, skill_name)
    (".claude/skills/nauro-adopt/SKILL.md", "claude_code", "nauro-adopt"),
    (".cursor/rules/nauro-adopt.mdc", "cursor", "nauro-adopt"),
    (".agents/skills/nauro-adopt/SKILL.md", "codex", "nauro-adopt"),
    (".claude/skills/nauro-ship-task/SKILL.md", "claude_code", "nauro-ship-task"),
    (".cursor/rules/nauro-ship-task.mdc", "cursor", "nauro-ship-task"),
    (".agents/skills/nauro-ship-task/SKILL.md", "codex", "nauro-ship-task"),
    (".claude/skills/nauro-context/SKILL.md", "claude_code", "nauro-context"),
    (".cursor/rules/nauro-context.mdc", "cursor", "nauro-context"),
    (".agents/skills/nauro-context/SKILL.md", "codex", "nauro-context"),
    (".claude/skills/nauro-loop/SKILL.md", "claude_code", "nauro-loop"),
    (".cursor/rules/nauro-loop.mdc", "cursor", "nauro-loop"),
    (".agents/skills/nauro-loop/SKILL.md", "codex", "nauro-loop"),
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
    content = load_docs_adopt_prompt()
    assert load_adopt_body() in content, (
        "docs/adopt-prompt.md does not contain load_adopt_body() — re-append "
        "or update the intro to keep the canonical body in sync."
    )


# --- Retired phrases must not reappear in skill / docs surfaces ---
#
# Each entry pairs a retired phrase with the reason it was retired, scanned
# across every surface in ``SKILL_SURFACES``; dogfood files inherit via the
# byte-equality test.

RETIRED_PHRASES = [
    ("LLM-based", "Tier 3 LLM validation was removed"),
    ("Tier 3", "Tier 3 LLM validation was removed"),
    ("nauro extract", "the extract command was retired"),
    ("[extraction]", "the [extraction] extra was retired"),
    ("Anthropic SDK", "Anthropic SDK was dropped as runtime dep"),
    ("Python 3.11+", "the Python floor was lowered to 3.10"),
    (
        "propose_decision(title, rationale, rejected,",
        "propose_decision became operation-aware; rejected/confidence are no longer positional",
    ),
    (
        "bracketed-prompt placeholders in `project.md` / `stack.md` / `state_current.md`",
        "bracket-prompt scaffolding was removed from state_current.md",
    ),
    (
        "The agent does not read source code, tests, IaC templates, or git history during adopt",
        "the docs-only stance was reversed — code is evidence on filesystem-capable surfaces",
    ),
    (
        "Step 5a — Clear decisions",
        "renamed to Step 6a — Documented decisions",
    ),
    (
        "Step 5b — Boundary candidates",
        "split into Step 6b (code-evidenced) + Step 6c (stack inventory)",
    ),
    (
        "confirm_decision",
        "confirm_decision was removed; propose_decision is now a single-call commit",
    ),
    (
        'gh pr create --body "',
        "inline PR bodies break on quote characters; the chain writes the body to a file",
    ),
    (
        "`mcp-server` consumes from `nauro-core`",
        "the always-gate triggers were generalized; the body must not name this project's repos",
    ),
    (
        "`v1` of every mode targets",
        "the internal roadmap label was removed; the body leads with the local-store mechanism",
    ),
]


@pytest.mark.parametrize("surface_name,loader", list(SKILL_SURFACES.items()))
@pytest.mark.parametrize("phrase,reason", RETIRED_PHRASES)
def test_skill_surface_has_no_retired_phrases(
    surface_name: str, loader, phrase: str, reason: str
) -> None:
    content = loader()
    assert phrase not in content, f"retired phrase {phrase!r} found in {surface_name}: {reason}"


# --- Cross-step reference integrity (adopt_body.md only) ---
#
# The adopt body has 15+ prose references like "Step 6a" and "Step 7 step 3".
# Renumbering a heading would silently rot every cross-ref. Guard by
# asserting every ``Step N[a-c]?`` ref resolves to an actual heading.


def _extract_step_id(text: str, start: int) -> str | None:
    """Read digits + optional ``a``/``b``/``c`` at ``start``; ``None`` if no digit."""
    i = start
    while i < len(text) and text[i].isdigit():
        i += 1
    if i == start:
        return None
    if i < len(text) and text[i] in "abc":
        i += 1
    return text[start:i]


def _iter_step_refs(text: str) -> Iterator[str]:
    """Yield each ``Step N[a-c]?`` mention found in prose."""
    needle = "Step "
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx < 0:
            return
        step_id = _extract_step_id(text, idx + len(needle))
        if step_id:
            yield step_id
        pos = idx + len(needle)


def _iter_step_headings(text: str) -> Iterator[str]:
    """Yield each step id from headings like ``## Step N — Title``."""
    for line in text.splitlines():
        stripped = line.lstrip("#").lstrip()
        if not stripped.startswith("Step "):
            continue
        step_id = _extract_step_id(stripped, len("Step "))
        if step_id:
            yield step_id


def test_adopt_body_step_references_resolve_to_headings():
    body = load_adopt_body()
    refs = set(_iter_step_refs(body))
    headings = set(_iter_step_headings(body))
    missing = refs - headings
    assert not missing, (
        f"adopt_body.md has Step references without matching headings: "
        f"{sorted(missing)}; headings present: {sorted(headings)}"
    )
