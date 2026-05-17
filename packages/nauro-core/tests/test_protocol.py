"""Module-internal invariants for ``nauro_core.protocol``.

Lives in nauro-core so the package is self-defending even when the downstream
``nauro`` test suite is not running. Rendered-surface drift checks live in
``packages/nauro/tests/test_protocol_drift.py``.
"""

from __future__ import annotations

import pytest

from nauro_core.constants import MCP_INSTRUCTIONS_STATIC
from nauro_core.protocol import (
    CANONICAL_FRAGMENTS,
    CHECK_DECISION_RETURNS,
    GET_DECISION_BEFORE_PROPOSING,
    NO_INVENT_RATIONALE,
    PROPOSE_DECISION_OPERATIONS,
    UPDATE_SUPERSEDE_CARE,
    protocol_tokens_in,
    substitute_protocol_fragments,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fragment anchor smoke tests — each fragment must say what its name claims
# ─────────────────────────────────────────────────────────────────────────────


class TestFragmentAnchors:
    def test_check_decision_returns_names_bm25_and_deterministic(self) -> None:
        assert "BM25" in CHECK_DECISION_RETURNS
        assert "deterministic" in CHECK_DECISION_RETURNS
        assert "does NOT judge conflicts" in CHECK_DECISION_RETURNS

    def test_get_decision_before_proposing_says_before_proposing(self) -> None:
        assert "`get_decision`" in GET_DECISION_BEFORE_PROPOSING
        assert "before proposing" in GET_DECISION_BEFORE_PROPOSING
        assert "supersession status" in GET_DECISION_BEFORE_PROPOSING

    def test_propose_decision_operations_names_all_three_and_metadata_rule(self) -> None:
        for op in ("add", "update", "supersede"):
            assert f"`{op}`" in PROPOSE_DECISION_OPERATIONS, op
        assert "`affected_decision_id`" in PROPOSE_DECISION_OPERATIONS
        # Update is rationale-only; metadata changes are rejected at the boundary
        assert "rationale-only" in PROPOSE_DECISION_OPERATIONS
        assert "server rejects" in PROPOSE_DECISION_OPERATIONS
        for field in (
            "`title`",
            "`confidence`",
            "`decision_type`",
            "`reversibility`",
            "`files_affected`",
            "`rejected`",
        ):
            assert field in PROPOSE_DECISION_OPERATIONS, field

    def test_update_supersede_care_defaults_to_add(self) -> None:
        assert "`add`" in UPDATE_SUPERSEDE_CARE
        assert "supersede" in UPDATE_SUPERSEDE_CARE
        assert "hard to reverse" in UPDATE_SUPERSEDE_CARE
        # Practical recovery guidance for an uncertain agent
        assert "a later proposal can update or supersede" in UPDATE_SUPERSEDE_CARE

    def test_no_invent_rationale_says_what_it_says(self) -> None:
        assert "invent rationale" in NO_INVENT_RATIONALE
        assert "actually decided" in NO_INVENT_RATIONALE
        assert "reasoning that supports it" in NO_INVENT_RATIONALE

    def test_no_invent_rationale_is_surface_neutral(self) -> None:
        """The fragment must not prescribe a specific evidence source (docs,
        probe answers, user confirmation). Those concepts are adopt-specific
        and would contradict MCP/session guidance to record decisions made
        during conversation."""
        for adopt_specific in (
            "source documents",
            "user confirms",
            "probe",
            "from code or prose",
        ):
            assert adopt_specific not in NO_INVENT_RATIONALE, (
                f"NO_INVENT_RATIONALE leaks adopt-specific framing: {adopt_specific!r}"
            )

    @pytest.mark.parametrize("name,value", list(CANONICAL_FRAGMENTS.items()))
    def test_each_fragment_is_non_empty(self, name: str, value: str) -> None:
        assert value.strip(), f"fragment {name!r} is empty or whitespace-only"

    @pytest.mark.parametrize("name,value", list(CANONICAL_FRAGMENTS.items()))
    def test_no_fragment_contains_protocol_token_prefix(self, name: str, value: str) -> None:
        """Backstop the module-load invariant: a fragment whose value contains
        a ``<!-- protocol:`` substring would chain on substitution. The runtime
        check in protocol.py raises ValueError at import; this test makes the
        invariant explicit and survives ``python -O`` where asserts would not.
        """
        assert "<!-- protocol:" not in value, f"fragment {name!r} contains a protocol token prefix"


# ─────────────────────────────────────────────────────────────────────────────
# Substitution behaviour
# ─────────────────────────────────────────────────────────────────────────────


class TestSubstitution:
    @pytest.mark.parametrize("name", list(CANONICAL_FRAGMENTS))
    def test_each_known_token_resolves(self, name: str) -> None:
        token = f"<!-- protocol:{name} -->"
        result = substitute_protocol_fragments(f"prefix {token} suffix")
        assert token not in result
        assert CANONICAL_FRAGMENTS[name] in result

    def test_unknown_token_is_left_intact(self) -> None:
        text = "before <!-- protocol:NOT_A_FRAGMENT --> after"
        assert substitute_protocol_fragments(text) == text

    def test_substitution_is_idempotent(self) -> None:
        text = (
            "intro <!-- protocol:CHECK_DECISION_RETURNS --> middle "
            "<!-- protocol:NO_INVENT_RATIONALE --> tail"
        )
        once = substitute_protocol_fragments(text)
        twice = substitute_protocol_fragments(once)
        assert once == twice

    def test_resolved_output_never_contains_token_prefix(self) -> None:
        text = " ".join(f"<!-- protocol:{name} -->" for name in CANONICAL_FRAGMENTS)
        assert "<!-- protocol:" not in substitute_protocol_fragments(text)

    def test_multiple_occurrences_of_same_token_all_resolve(self) -> None:
        token = "<!-- protocol:CHECK_DECISION_RETURNS -->"
        text = f"a {token} b {token} c"
        out = substitute_protocol_fragments(text)
        assert token not in out
        assert out.count(CHECK_DECISION_RETURNS) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Unknown-token detection — supports source-template typo guards
# ─────────────────────────────────────────────────────────────────────────────


class TestProtocolTokensIn:
    def test_finds_known_and_unknown_by_default(self) -> None:
        text = "<!-- protocol:CHECK_DECISION_RETURNS --> and <!-- protocol:MISTYPED_NAME -->"
        assert protocol_tokens_in(text) == [
            "CHECK_DECISION_RETURNS",
            "MISTYPED_NAME",
        ]

    def test_only_unknown_filters_known(self) -> None:
        text = "<!-- protocol:CHECK_DECISION_RETURNS --> and <!-- protocol:MISTYPED_NAME -->"
        assert protocol_tokens_in(text, only_unknown=True) == ["MISTYPED_NAME"]

    def test_empty_when_no_tokens(self) -> None:
        assert protocol_tokens_in("plain text with no tokens") == []
        assert protocol_tokens_in("", only_unknown=True) == []

    def test_malformed_token_without_suffix_is_ignored(self) -> None:
        # An open prefix with no closing " -->" should not loop or raise.
        text = "<!-- protocol:CHECK_DECISION_RETURNS broken"
        assert protocol_tokens_in(text) == []


# ─────────────────────────────────────────────────────────────────────────────
# MCP_INSTRUCTIONS_STATIC composition contract
# ─────────────────────────────────────────────────────────────────────────────


class TestMcpInstructionsComposition:
    """MCP_INSTRUCTIONS_STATIC must contain every fragment used by the MCP
    surface verbatim, and must be fully resolved (no leftover tokens)."""

    REQUIRED_FRAGMENTS = (
        CHECK_DECISION_RETURNS,
        GET_DECISION_BEFORE_PROPOSING,
        PROPOSE_DECISION_OPERATIONS,
        UPDATE_SUPERSEDE_CARE,
        NO_INVENT_RATIONALE,
    )

    @pytest.mark.parametrize("fragment", REQUIRED_FRAGMENTS)
    def test_mcp_static_contains_fragment_verbatim(self, fragment: str) -> None:
        assert fragment in MCP_INSTRUCTIONS_STATIC

    def test_mcp_static_has_no_unresolved_tokens(self) -> None:
        assert protocol_tokens_in(MCP_INSTRUCTIONS_STATIC) == []
