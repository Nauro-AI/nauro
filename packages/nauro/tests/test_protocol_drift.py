"""Rendered-surface drift tests for canonical protocol fragments.

The canonical wording lives in ``nauro_core.protocol``. Module-internal
invariants (anchor substrings, substitution semantics, MCP composition) are
tested in ``packages/nauro-core/tests/test_protocol.py`` so nauro-core is
self-defending. This module covers the *rendered* surfaces that consume those
fragments: the two skill bodies after substitution, the 6 dogfood files, and
the ``docs/adopt-prompt.md`` chat-paste artifact.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
from nauro_core import (
    CHECK_DECISION_RETURNS,
    GET_DECISION_BEFORE_PROPOSING,
    NO_INVENT_RATIONALE,
)
from nauro_core.protocol import protocol_tokens_in

from nauro.skills import load_adopt_body, load_session_body, render_skill
from tests._skill_surfaces import REPO_ROOT, load_docs_adopt_prompt

# Per-surface fragment manifest. Each rendered surface must contain every
# declared fragment value verbatim. Fragments that a surface deliberately
# omits (e.g. session today does not surface propose_decision operations)
# stay out of this map — silence is allowed, divergence is not.
SURFACE_FRAGMENTS: dict[str, list[str]] = {
    "session": [
        CHECK_DECISION_RETURNS,
        GET_DECISION_BEFORE_PROPOSING,
        NO_INVENT_RATIONALE,
    ],
    "adopt": [
        CHECK_DECISION_RETURNS,
        GET_DECISION_BEFORE_PROPOSING,
        NO_INVENT_RATIONALE,
    ],
}

# Adopt's Step 7 step 3 restates D131/D133 operation semantics in its own
# numbered-step + sub-bullet shape; the bullet-form PROPOSE_DECISION_OPERATIONS
# fragment cannot be substituted there without breaking markdown indentation.
# To prevent operation-content drift between MCP and adopt, assert adopt's
# rendered body still mentions every load-bearing anchor from the fragment.
ADOPT_OPERATION_ANCHORS: tuple[str, ...] = (
    # The three operation names (backtick-quoted in the fragment; adopt uses
    # both `op` and **op** spellings, so the substring check works on either)
    "add",
    "update",
    "supersede",
    # The kwarg every mutation requires
    "affected_decision_id",
    # The decision number that introduced the metadata-rejection rule
    "D133",
    # The six metadata fields rejected at the boundary on operation=update
    "title",
    "confidence",
    "decision_type",
    "reversibility",
    "files_affected",
    "rejected",
)


# ─────────────────────────────────────────────────────────────────────────────
# Surface-fragment containment — every declared fragment appears verbatim
# ─────────────────────────────────────────────────────────────────────────────


def _session_rendered() -> str:
    return render_skill("claude_code", "nauro")


def _adopt_rendered() -> str:
    return render_skill("claude_code", "nauro-adopt")


_RENDERED_LOADERS = {"session": _session_rendered, "adopt": _adopt_rendered}


@pytest.mark.parametrize(
    "surface,fragment",
    [
        (surface, fragment)
        for surface, fragments in SURFACE_FRAGMENTS.items()
        for fragment in fragments
    ],
)
def test_rendered_surface_contains_fragment_verbatim(surface: str, fragment: str) -> None:
    rendered = _RENDERED_LOADERS[surface]()
    assert fragment in rendered, (
        f"surface {surface!r} is missing canonical fragment verbatim "
        f"(first 80 chars): {fragment[:80]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Adopt operations parity — content equivalence without bullet-form tokenization
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("anchor", ADOPT_OPERATION_ANCHORS)
def test_adopt_body_mentions_operation_anchor(anchor: str) -> None:
    """If adopt's Step 7 ever stops mentioning one of these anchors, the
    operation-semantics drift the protocol-fragment refactor was meant to
    close has reopened in adopt-specific prose. Either re-add the anchor or
    rework the fragment substitution strategy for the bullet-list case."""
    body = load_adopt_body()
    assert anchor in body, (
        f"adopt body missing operation anchor {anchor!r} — see "
        "PROPOSE_DECISION_OPERATIONS in nauro_core.protocol for the canonical "
        "content adopt's Step 7 must keep in parity with."
    )


# ─────────────────────────────────────────────────────────────────────────────
# No unresolved tokens in any rendered surface
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "surface,skill",
    [(s, k) for s in ("claude_code", "cursor", "codex") for k in ("nauro", "nauro-adopt")],
)
def test_rendered_skill_has_no_protocol_tokens(surface: str, skill: str) -> None:
    rendered = render_skill(surface, skill)
    assert protocol_tokens_in(rendered) == [], (
        f"render_skill({surface!r}, {skill!r}) leaked unresolved tokens"
    )


def test_loaders_have_no_protocol_tokens() -> None:
    assert protocol_tokens_in(load_session_body()) == []
    assert protocol_tokens_in(load_adopt_body()) == []


def test_docs_adopt_prompt_has_no_protocol_tokens() -> None:
    """``docs/adopt-prompt.md`` is a distribution artifact — committed fully
    rendered, so users pasting it from GitHub never see template internals.
    """
    assert protocol_tokens_in(load_docs_adopt_prompt()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Source templates: only known tokens allowed (catches typos at the source)
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_TEMPLATE_FILES = ("session_body.md", "adopt_body.md")


@pytest.mark.parametrize("template_filename", SOURCE_TEMPLATE_FILES)
def test_source_template_has_only_known_tokens(template_filename: str) -> None:
    """A typo like ``<!-- protocol:CHECK_DECSION_RETURNS -->`` (missing ``I``)
    must fail here pointing at the source ``.md`` file, not silently render to
    a broken claim that an agent reads in production."""
    raw = resources.files("nauro.skills").joinpath(template_filename).read_text(encoding="utf-8")
    unknown = protocol_tokens_in(raw, only_unknown=True)
    assert not unknown, (
        f"{template_filename} references unknown protocol tokens: {unknown}. "
        "Check spelling against nauro_core.protocol.CANONICAL_FRAGMENTS."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Distribution artifacts must contain zero protocol-token substrings
# ─────────────────────────────────────────────────────────────────────────────

# The 6 dogfood files mirror the parametrization in test_skills_drift; keep in
# sync. The byte-equality test in test_skills_drift already pins them to
# render_skill() output, so the token-free assertion here is belt-and-braces.
DISTRIBUTION_FILES = (
    ".claude/skills/nauro-adopt/SKILL.md",
    ".claude/skills/nauro/SKILL.md",
    ".cursor/rules/nauro-adopt.mdc",
    ".cursor/rules/nauro.mdc",
    ".agents/skills/nauro-adopt/SKILL.md",
    ".agents/skills/nauro/SKILL.md",
    "docs/adopt-prompt.md",
)


@pytest.mark.parametrize("rel_path", DISTRIBUTION_FILES)
def test_distribution_artifact_is_token_free(rel_path: str) -> None:
    file_path: Path = REPO_ROOT / rel_path
    assert file_path.is_file(), f"missing distribution artifact: {file_path}"
    content = file_path.read_text(encoding="utf-8")
    assert "<!-- protocol:" not in content, (
        f"{rel_path} contains a raw protocol token — distribution artifacts "
        "must ship fully resolved. Regenerate via render_skill()."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Retired paraphrases — pre-refactor wording must not reappear
# ─────────────────────────────────────────────────────────────────────────────
#
# Each entry is an exact substring from the pre-refactor surfaces. If a future
# edit reintroduces one of these phrasings, drift has crept back and the
# canonical fragment in nauro_core.protocol should be used instead.

RETIRED_PARAPHRASES = (
    # Pre-refactor MCP wording for CHECK_DECISION_RETURNS
    (
        "surfaced via BM25 retrieval and a deterministic assessment. "
        "It does NOT judge conflicts for you"
    ),
    # Pre-refactor session_body wording for CHECK_DECISION_RETURNS +
    # GET_DECISION_BEFORE_PROPOSING merged into one sentence
    "does not judge conflicts for the agent — when the response lists",
    # Pre-refactor adopt_body wording for the same two claims
    "`check_decision` does not judge conflicts — the agent reads the decision bodies",
    # Pre-refactor adopt_body intro wording for NO_INVENT_RATIONALE
    "it does not invent rationale from code or prose",
)


@pytest.mark.parametrize(
    "surface_name,getter",
    [
        ("session (rendered)", _session_rendered),
        ("adopt (rendered)", _adopt_rendered),
        ("docs/adopt-prompt.md", load_docs_adopt_prompt),
    ],
)
@pytest.mark.parametrize("paraphrase", RETIRED_PARAPHRASES)
def test_retired_paraphrase_absent(surface_name: str, getter, paraphrase: str) -> None:
    content = getter()
    assert paraphrase not in content, (
        f"retired paraphrase reintroduced in {surface_name}: {paraphrase[:80]!r}. "
        "Use the canonical fragment from nauro_core.protocol instead."
    )
