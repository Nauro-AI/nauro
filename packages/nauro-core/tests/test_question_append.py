"""Tests for nauro_core.question_append — the shared entry composer.

``flag_question`` and the hosted shared-context workflow both enter new
``open-questions.md`` entries through these helpers; the parity pins in
``tests/operations/test_operations_flag_question.py`` cover the wired
behavior, these pin the helpers' own contract.
"""

from __future__ import annotations

from nauro_core.question_append import (
    allocate_question_number,
    compose_question_entry,
    insert_question_entry,
)
from nauro_core.questions import OpenQuestionsFile


class TestAllocateQuestionNumber:
    def test_empty_file_allocates_one(self) -> None:
        parsed = OpenQuestionsFile.parse("# Open Questions\n")
        assert allocate_question_number(parsed) == 1

    def test_next_after_highest_existing(self) -> None:
        content = "# Open Questions\n- [Q3] Three?\n- [Q7] Seven?\n- [Q2] Two?\n"
        parsed = OpenQuestionsFile.parse(content)
        assert allocate_question_number(parsed) == 8

    def test_resolved_entries_still_reserve_their_numbers(self) -> None:
        # A resolved Q keeps its number reserved: allocation counts every
        # numbered entry in the file, resolved history included.
        content = (
            "# Open Questions\n"
            "- [Q1] Open one?\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D9 on 2026-05-01] [Q5] Done?\n"
        )
        parsed = OpenQuestionsFile.parse(content)
        assert allocate_question_number(parsed) == 6

    def test_legacy_timestamp_entries_do_not_number(self) -> None:
        content = "# Open Questions\n- [2026-05-01 12:00 UTC] Legacy form?\n"
        parsed = OpenQuestionsFile.parse(content)
        assert allocate_question_number(parsed) == 1


class TestComposeQuestionEntry:
    def test_canonical_line_form(self) -> None:
        assert compose_question_entry(12, "Should we cache?") == "- [Q12] Should we cache?"

    def test_pointer_body_composes_identically(self) -> None:
        body = "BRIEF: context/auth-cutover.md - the auth cutover plan"
        assert compose_question_entry(3, body) == f"- [Q3] {body}"


class TestInsertQuestionEntry:
    def test_inserts_directly_after_header(self) -> None:
        content = "# Open Questions\n- [Q1] First?\n"
        result = insert_question_entry(content, "- [Q2] Second?")
        assert result == "# Open Questions\n- [Q2] Second?\n- [Q1] First?\n"

    def test_skips_blank_lines_and_leading_comment(self) -> None:
        content = "# Open Questions\n\n<!-- guidance comment -->\n- [Q1] First?\n"
        result = insert_question_entry(content, "- [Q2] Second?")
        assert result == (
            "# Open Questions\n\n<!-- guidance comment -->\n- [Q2] Second?\n- [Q1] First?\n"
        )

    def test_headerless_file_inserts_at_second_line(self) -> None:
        content = "no header here\nbody line\n"
        result = insert_question_entry(content, "- [Q1] First?")
        assert result == "no header here\n- [Q1] First?\nbody line\n"
