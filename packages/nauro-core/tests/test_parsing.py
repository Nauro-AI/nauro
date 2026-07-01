"""Tests for nauro_core.parsing."""

import pytest

from nauro_core.constants import DECISIONS_DIR
from nauro_core.parsing import (
    _canonical_decision_id,
    _decision_filename,
    _decision_label,
    _decision_number_prefix,
    _decision_path,
    _stem_from_decision_path,
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


# ── Decision id / filename formatters ──
#
# Byte-identity guard for the module-private formatters extracted from the
# operations kernel. Each assertion pins the helper output against the exact
# inline expression it replaced at its former call site, so a future edit to a
# helper that drifts from that spelling fails here rather than silently at a
# call site.

_FORMATTER_NUMBERS = [0, 1, 5, 42, 100, 999]


class TestDecisionNumberFormatters:
    @pytest.mark.parametrize("num", _FORMATTER_NUMBERS)
    def test_canonical_decision_id_matches_inline(self, num: int) -> None:
        assert _canonical_decision_id(num) == f"decision-{num:03d}"

    @pytest.mark.parametrize("num", _FORMATTER_NUMBERS)
    def test_decision_label_matches_inline(self, num: int) -> None:
        assert _decision_label(num) == f"D{num:03d}"

    @pytest.mark.parametrize("num", _FORMATTER_NUMBERS)
    def test_decision_number_prefix_matches_inline(self, num: int) -> None:
        assert _decision_number_prefix(num) == f"{num:03d}-"


class TestDecisionFilenameFormatters:
    @pytest.mark.parametrize(
        "stem",
        ["001-foo", "042-use-postgres", "999-z", ""],
    )
    def test_decision_filename_matches_inline(self, stem: str) -> None:
        assert _decision_filename(stem) == f"{stem}.md"

    @pytest.mark.parametrize(
        "stem",
        ["001-foo", "042-use-postgres", "999-z", ""],
    )
    def test_decision_path_matches_inline(self, stem: str) -> None:
        assert _decision_path(stem) == f"{DECISIONS_DIR}/{stem}.md"


def _old_decision_stem(path: str) -> str | None:
    """Reference copy of the former ``_in_memory_store._decision_stem`` logic."""
    prefix = f"{DECISIONS_DIR}/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix) :]
    if "/" in tail or not tail.endswith(".md"):
        return None
    return tail[: -len(".md")]


class TestStemFromDecisionPath:
    @pytest.mark.parametrize(
        "path",
        [
            f"{DECISIONS_DIR}/001-foo.md",
            f"{DECISIONS_DIR}/042-use-postgres.md",
            f"{DECISIONS_DIR}/.md",
            f"{DECISIONS_DIR}/001-foo",
            f"{DECISIONS_DIR}/sub/001-foo.md",
            f"{DECISIONS_DIR}/001-foo.txt",
            "001-foo.md",
            "state_current.md",
            "",
        ],
    )
    def test_matches_old_logic(self, path: str) -> None:
        assert _stem_from_decision_path(path) == _old_decision_stem(path)

    def test_prefixed_with_md_returns_stem(self) -> None:
        path = f"{DECISIONS_DIR}/042-use-postgres.md"
        assert _stem_from_decision_path(path) == "042-use-postgres"

    def test_without_prefix_returns_none(self) -> None:
        assert _stem_from_decision_path("042-use-postgres.md") is None

    def test_prefixed_without_md_returns_none(self) -> None:
        assert _stem_from_decision_path(f"{DECISIONS_DIR}/042-use-postgres") is None

    def test_nested_path_returns_none(self) -> None:
        assert _stem_from_decision_path(f"{DECISIONS_DIR}/sub/042.md") is None
