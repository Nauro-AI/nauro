"""Tests for nauro_core.constants — sanity checks."""

from nauro_core.constants import (
    DECISION_HASHES_FILE,
    DECISION_TYPES,
    DECISIONS_DIR,
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
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
