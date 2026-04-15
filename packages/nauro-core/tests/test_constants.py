"""Tests for nauro_core.constants — sanity checks."""

from nauro_core.constants import (
    DECISION_HASHES_FILE,
    DECISION_TYPES,
    DECISIONS_DIR,
    EXTRACTION_SOURCES,
    JACCARD_THRESHOLD,
    L0_DECISIONS_SUMMARY_LIMIT,
    L0_QUESTIONS_LIMIT,
    L1_DECISIONS_LIMIT,
    L1_DECISIONS_SUMMARY_LIMIT,
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
        assert L0_DECISIONS_SUMMARY_LIMIT > 0

    def test_l0_questions_limit_positive(self):
        assert L0_QUESTIONS_LIMIT > 0

    def test_l1_decisions_limit_positive(self):
        assert L1_DECISIONS_LIMIT > 0

    def test_l1_decisions_summary_limit_positive(self):
        assert L1_DECISIONS_SUMMARY_LIMIT > 0

    def test_min_rationale_length_positive(self):
        assert MIN_RATIONALE_LENGTH > 0


class TestThresholds:
    def test_jaccard_threshold_in_range(self):
        assert 0.0 < JACCARD_THRESHOLD <= 1.0


class TestValidValues:
    def test_valid_confidences_non_empty(self):
        assert len(VALID_CONFIDENCES) > 0

    def test_valid_confidences_contains_expected(self):
        assert "high" in VALID_CONFIDENCES
        assert "medium" in VALID_CONFIDENCES
        assert "low" in VALID_CONFIDENCES

    def test_decision_types_non_empty(self):
        assert len(DECISION_TYPES) > 0

    def test_reversibility_levels_non_empty(self):
        assert len(REVERSIBILITY_LEVELS) > 0

    def test_extraction_sources_non_empty(self):
        assert len(EXTRACTION_SOURCES) > 0


class TestFilenames:
    def test_store_filenames_are_strings(self):
        names = [PROJECT_MD, STATE_MD, STACK_MD, OPEN_QUESTIONS_MD, DECISIONS_DIR, SNAPSHOTS_DIR]
        for name in names:
            assert isinstance(name, str)
            assert len(name) > 0

    def test_decision_hashes_file_is_json(self):
        assert DECISION_HASHES_FILE.endswith(".json")
