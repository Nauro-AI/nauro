"""Tests for nauro_core.constants — sanity checks."""

from nauro_core.constants import (
    DECISION_HASHES_FILE,
    DECISION_TYPES,
    DECISIONS_DIR,
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
    MAX_BRIEF_BYTES,
    MCP_INSTRUCTIONS,
    MCP_INSTRUCTIONS_STATIC,
    MIN_RATIONALE_LENGTH,
    OPEN_QUESTIONS_MD,
    PROJECT_MD,
    REVERSIBILITY_LEVELS,
    SNAPSHOTS_DIR,
    STACK_MD,
    STATE_MD,
    VALID_CONFIDENCES,
)
from nauro_core.instructions import build_remote_instructions
from nauro_core.protocol import (
    GET_DECISION_BEFORE_PROPOSING,
    PROPOSE_DECISION_OPERATIONS,
    RESOLVES_OPEN_QUESTIONS,
)

# The static instruction block must stay under the claude.ai
# initialize.instructions truncation point (~2,023 chars) with room for the
# per-user project section the remote server prepends. Trimming the trailing
# update-state and get-context-followup guidance — now carried on the
# matching ToolSpec descriptions, which tools/list delivers intact — brought
# the block back under the cliff. This is the post-trim ceiling: modest
# headroom above the current length so future growth past the cliff forces a
# conscious bump and a re-check that the composed remote payload still keeps
# every section header under the truncation point.
MCP_INSTRUCTIONS_TRUNCATION_LIMIT = 2023
MCP_INSTRUCTIONS_STATIC_MAX_CHARS = 1891


class TestLimits:
    def test_l0_decisions_summary_limit_positive(self):
        assert L0_DECISIONS_SUMMARY_LIMIT == 10

    def test_l0_questions_limit_positive(self):
        assert L0_QUESTIONS_LIMIT == 3

    def test_l1_decisions_limit_positive(self):
        assert L1_DECISIONS_LIMIT == 10

    def test_l1_decisions_summary_limit_positive(self):
        assert L1_DECISIONS_SUMMARY_LIMIT == 10

    def test_min_rationale_length_positive(self):
        assert MIN_RATIONALE_LENGTH == 20


class TestSizeLimits:
    def test_max_brief_bytes_is_50_kib(self):
        """The shared-brief cap is pinned at 50 KiB. The sync push-time
        warn-and-skip gate and the nauro-context skill prose both reference
        this single value, so a careless change must trip a test."""
        assert MAX_BRIEF_BYTES == 50 * 1024


class TestValidValues:
    def test_valid_confidences_non_empty(self):
        assert {"high", "medium", "low"} == VALID_CONFIDENCES

    def test_valid_confidences_contains_expected(self):
        assert "high" in VALID_CONFIDENCES
        assert "medium" in VALID_CONFIDENCES
        assert "low" in VALID_CONFIDENCES

    def test_decision_types_non_empty(self):
        assert DECISION_TYPES == (
            "architecture",
            "library_choice",
            "pattern",
            "refactor",
            "api_design",
            "infrastructure",
            "data_model",
        )

    def test_reversibility_levels_non_empty(self):
        assert REVERSIBILITY_LEVELS == ("easy", "moderate", "hard")


class TestFilenames:
    def test_store_filenames_are_strings(self):
        assert PROJECT_MD == "project.md"
        assert STATE_MD == "state.md"
        assert STACK_MD == "stack.md"
        assert OPEN_QUESTIONS_MD == "open-questions.md"
        assert DECISIONS_DIR == "decisions"
        assert SNAPSHOTS_DIR == "snapshots"

    def test_decision_hashes_file_is_json(self):
        assert DECISION_HASHES_FILE == ".decision-hashes.json"


class TestMcpInstructions:
    def test_check_decision_section_is_a_precondition(self):
        """The check_decision guidance must explicitly forbid the skip-on-rejection
        loophole. A competent agent with a strong premise to attack can otherwise
        reason past the tool entirely.
        """
        assert "precondition, not an option" in MCP_INSTRUCTIONS_STATIC
        assert "first-principles reasoning is not a substitute" in MCP_INSTRUCTIONS_STATIC

    def test_check_decision_triggers_on_rejection_too(self):
        """The directive must apply even when the agent intends to push back —
        not only when it intends to adopt the proposed approach.
        """
        assert "push back" in MCP_INSTRUCTIONS_STATIC

    def test_check_decision_lists_vendor_swap(self):
        """Vendor swaps are a common conflict surface (e.g. S3 ↔ R2)."""
        assert "vendor swap" in MCP_INSTRUCTIONS_STATIC

    def test_propose_decision_operations_not_in_static(self) -> None:
        """Relocated to the propose_decision.operation parameter so the
        static block stays small enough for the per-user project section the
        remote server prepends to survive client-side truncation of the
        ``initialize.instructions`` field."""
        assert PROPOSE_DECISION_OPERATIONS not in MCP_INSTRUCTIONS_STATIC

    def test_resolves_open_questions_not_in_static(self) -> None:
        """Relocated to the propose_decision.resolves_questions parameter
        description, same budget reason as the operations fragment."""
        assert RESOLVES_OPEN_QUESTIONS not in MCP_INSTRUCTIONS_STATIC

    def test_static_block_does_not_exceed_budget_ceiling(self) -> None:
        """Regression guard: the static block must not grow past its
        post-trim ceiling. Future edits that would enlarge the block force a
        conscious bump of the ceiling and a re-check that the remote
        per-user section still survives truncation."""
        assert len(MCP_INSTRUCTIONS_STATIC) <= MCP_INSTRUCTIONS_STATIC_MAX_CHARS

    def test_budget_ceiling_under_truncation_limit(self) -> None:
        """The ceiling itself must stay under the claude.ai cliff so the
        static block always leaves headroom for the prepended per-user
        project section. A bump that pushed the ceiling past the cliff would
        re-introduce the truncation that drops trailing sections."""
        assert MCP_INSTRUCTIONS_STATIC_MAX_CHARS < MCP_INSTRUCTIONS_TRUNCATION_LIMIT

    def test_update_state_section_not_in_static(self) -> None:
        """The 'When to update state' section was trimmed from the static
        block; its canonical home is the update_state ToolSpec description,
        which tools/list delivers intact past the truncation point."""
        assert "## When to update state" not in MCP_INSTRUCTIONS_STATIC

    def test_list_decisions_followup_nuance_not_in_static(self) -> None:
        """The 'do not call list_decisions after get_context' nuance was the
        truncation-risk tail of the get-context section; it now lives on the
        get_context ToolSpec description instead of the static block."""
        assert "list_decisions" not in MCP_INSTRUCTIONS_STATIC

    def test_local_and_remote_share_static_tail(self) -> None:
        """The local stdio server delivers MCP_INSTRUCTIONS verbatim; the
        remote server composes MCP_INSTRUCTIONS_STATIC into a per-user
        payload. Both draw from the same static tail, so the two surfaces
        cannot silently drift. The alias pins that single source."""
        assert MCP_INSTRUCTIONS == MCP_INSTRUCTIONS_STATIC
        remote = build_remote_instructions(MCP_INSTRUCTIONS_STATIC, [])
        assert remote.endswith(MCP_INSTRUCTIONS)

    def test_header_first_hydration_fragment_present_and_bounded(self) -> None:
        """The reworded hydration sentence is spliced into the static block
        and stays compact. The leading fetch mandate must precede the
        mode guidance so the sentence cannot read as "skip fetching"."""
        assert GET_DECISION_BEFORE_PROPOSING in MCP_INSTRUCTIONS_STATIC
        assert "`mode=header`" in MCP_INSTRUCTIONS_STATIC
        assert GET_DECISION_BEFORE_PROPOSING.index("before proposing") < (
            GET_DECISION_BEFORE_PROPOSING.index("`mode=header`")
        )
        # The reword must not exceed the original sentence's length, so the
        # static block holds at or below its pre-change ceiling.
        assert len(GET_DECISION_BEFORE_PROPOSING) <= 200
