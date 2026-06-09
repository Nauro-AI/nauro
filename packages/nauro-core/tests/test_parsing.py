"""Tests for nauro_core.parsing."""

from nauro_core.parsing import (
    extract_decision_number,
    first_sentence_end,
    scan_decision_references,
)


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


class TestFirstSentenceEnd:
    def test_simple_sentence(self):
        text = "First sentence here. Second one."
        assert text[: first_sentence_end(text)] == "First sentence here."

    def test_no_terminator_returns_full_length(self):
        text = "No terminator at all"
        assert first_sentence_end(text) == len(text)

    def test_terminator_at_end_of_text(self):
        text = "Just one sentence."
        assert first_sentence_end(text) == len(text)

    def test_decimal_point_not_a_boundary(self):
        text = "The ratio is 3.14 in this case. Next."
        assert text[: first_sentence_end(text)] == "The ratio is 3.14 in this case."

    def test_eg_abbreviation_not_a_boundary(self):
        text = "Should we e.g. cache the index?"
        assert text[: first_sentence_end(text)] == "Should we e.g. cache the index?"

    def test_ie_abbreviation_not_a_boundary(self):
        text = "Use one store, i.e. the local one. Done."
        assert text[: first_sentence_end(text)] == "Use one store, i.e. the local one."

    def test_vs_abbreviation_not_a_boundary(self):
        text = "Local vs. remote first. Then decide."
        assert text[: first_sentence_end(text)] == "Local vs. remote first."

    def test_single_letter_initial_not_a_boundary(self):
        text = "Authored by T. Thomsen here. Next sentence."
        assert text[: first_sentence_end(text)] == "Authored by T. Thomsen here."

    def test_question_and_exclamation_terminate(self):
        assert "Why?"[: first_sentence_end("Why? Because.")] == "Why?"
        assert "Stop!"[: first_sentence_end("Stop! Now.")] == "Stop!"


class TestScanDecisionReferences:
    def test_plain_d_form(self):
        assert scan_decision_references("As in D7 we agreed.", 100) == {7}

    def test_zero_padded_form(self):
        assert scan_decision_references("See D007 again.", 100) == {7}

    def test_decision_hyphen_lowercase(self):
        assert scan_decision_references("Per decision-7 last week.", 100) == {7}

    def test_decision_hyphen_capital_d(self):
        # extract_decision_number accepts "Decision-70"; the scanner agrees.
        assert scan_decision_references("Per Decision-70 last week.", 100) == {70}

    def test_lowercase_d_form(self):
        assert scan_decision_references("see d70 there", 100) == {70}

    def test_prefix_collision_full_run_read(self):
        # The only "D1" in the body is the prefix of "D118"; reading the whole
        # digit run yields 118, never a spurious 1.
        assert scan_decision_references("only D118 here", 200) == {118}

    def test_letter_preceded_token_rejected(self):
        # "keyID70" must not yield D70; the char before "D" is a letter.
        assert scan_decision_references("keyID70 is an identifier", 100) == set()

    def test_uuid_digit_run_rejected(self):
        # A UUID4 substring "...d4..." must not yield D4; the char before is a
        # hex digit. This is one of the live phantom-edge cases.
        assert scan_decision_references("uuid 7c9e6679-7425-40de-944b-e07fc1f90ae7", 100) == set()

    def test_ulid_substring_rejected(self):
        # A ULID like "01ARZ3NDEKTSV4RRFFQ69G5FAV" embeds "d..." runs preceded by
        # alphanumerics; none should surface as a reference.
        assert scan_decision_references("id 01ARZ3NDEKTSV4RRFFQ69G5FAV6", 100) == set()

    def test_digit_preceded_token_rejected(self):
        # "12D70" — the "D70" is preceded by a digit, so it is the tail of a
        # longer token and must not match.
        assert scan_decision_references("12D70 stuck together", 100) == set()

    def test_out_of_range_dropped(self):
        assert scan_decision_references("D999 and D0 are out of range", 100) == set()

    def test_unicode_digit_does_not_crash(self):
        # A superscript footnote digit after the number must not reach int();
        # only ASCII digits are consumed, so "D118¹" parses as 118.
        assert scan_decision_references("see D118¹ footnote", 200) == {118}

    def test_multiple_references(self):
        assert scan_decision_references("D1 and D2 and decision-3", 100) == {1, 2, 3}
