"""Tests for nauro_core.parsing."""

from nauro_core.parsing import extract_decision_number


class TestExtractDecisionNumber:
    def test_file_stem(self):
        assert extract_decision_number("042-some-title") == 42

    def test_file_stem_with_md_suffix(self):
        assert extract_decision_number("042-some-title.md") == 42

    def test_synthetic_decision_id(self):
        assert extract_decision_number("decision-042") == 42

    def test_synthetic_decision_id_no_padding(self):
        assert extract_decision_number("decision-42") == 42

    def test_d_prefixed_padded(self):
        assert extract_decision_number("D042") == 42

    def test_d_prefixed_unpadded(self):
        assert extract_decision_number("D42") == 42

    def test_lowercase_d(self):
        assert extract_decision_number("d042") == 42

    def test_bare_integer(self):
        assert extract_decision_number("42") == 42

    def test_bare_padded(self):
        assert extract_decision_number("042") == 42

    def test_garbage_returns_none(self):
        assert extract_decision_number("not-a-decision") is None

    def test_empty_returns_none(self):
        assert extract_decision_number("") is None

    def test_decision_without_number_returns_none(self):
        assert extract_decision_number("decision-") is None
