"""Tests for nauro_core.questions."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from nauro_core.questions import (
    EntryBlock,
    HeaderBlock,
    MigrationRename,
    OpenQuestionsFile,
    ProseBlock,
    QuestionEntry,
    ResolvedRef,
    TripleHashBlock,
    UnparsableBlock,
)


class TestResolvedRefValidation:
    def test_decision_num_must_be_positive(self):
        with pytest.raises(ValueError):
            ResolvedRef(decision_num=0, date=date(2026, 5, 14))


class TestParseRoundTrip:
    def test_empty_file_yields_default_header(self):
        file = OpenQuestionsFile.parse("")
        assert file.header == "# Open Questions"
        assert file.blocks == []

    def test_open_only(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1 body\n"
            "- [2026-05-11 15:29 UTC] q2 body\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["2026-05-12 20:18 UTC", "2026-05-11 15:29 UTC"]
        assert file.resolved_ids == []

    def test_open_and_resolved_split(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] still open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["2026-05-12 20:18 UTC"]
        assert file.resolved_ids == ["2026-04-30 10:00 UTC"]
        resolved_block = [b for b in file.blocks if isinstance(b, EntryBlock)][1]
        assert resolved_block.entry.resolved_by == ResolvedRef(
            decision_num=100, date=date(2026, 5, 1)
        )

    def test_continuation_lines(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "  Context: extra detail\n"
            "  Another continuation line\n"
            "- [2026-05-11 15:29 UTC] q2\n"
        )
        file = OpenQuestionsFile.parse(content)
        entry_blocks = [b for b in file.blocks if isinstance(b, EntryBlock)]
        assert entry_blocks[0].entry.continuation == [
            "  Context: extra detail",
            "  Another continuation line",
        ]
        assert entry_blocks[1].entry.continuation == []

    def test_intro_text_preserved(self):
        content = "# Open Questions\n\nFree-form intro paragraph.\n\n- [2026-05-12 20:18 UTC] q1\n"
        file = OpenQuestionsFile.parse(content)
        rendered = file.format()
        assert "Free-form intro paragraph." in rendered

    def test_round_trip_idempotent(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "  Context: detail\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        file = OpenQuestionsFile.parse(content)
        rendered = file.format()
        re_parsed = OpenQuestionsFile.parse(rendered)
        assert re_parsed == file

    def test_malformed_timestamp_skipped(self):
        content = (
            "# Open Questions\n\n- [not-a-timestamp] junk line\n- [2026-05-12 20:18 UTC] valid q\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["2026-05-12 20:18 UTC"]


class TestRoundTripFidelity:
    """Byte-identical round-trip cases for inputs that don't go through resolve."""

    def test_round_trip_blank_lines_between_entries(self):
        content = (
            "# Open Questions\n\n- [2026-05-12 20:18 UTC] q1\n\n\n- [2026-05-11 15:29 UTC] q2\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_round_trip_prose_after_first_entry(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "\n"
            "Some commentary inserted by a human.\n"
            "\n"
            "- [2026-05-11 15:29 UTC] q2\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_round_trip_unparseable_dash_bracket_line(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "- [not-a-timestamp] garbage but please keep\n"
            "- [2026-05-11 15:29 UTC] q2\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_round_trip_open_form_under_resolved_header(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] still open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [2026-04-30 10:00 UTC] open-form entry that lives here\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_round_trip_triple_hash_topic_entries(self):
        content = (
            "# Open Questions\n"
            "\n"
            "### Topic: deployment\n"
            "Some topic narrative.\n"
            "More narrative.\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_round_trip_triple_hash_with_embedded_timestamp_id(self):
        content = (
            "# Open Questions\n"
            "\n"
            "### [2026-05-10 09:00 UTC] Topic: deployment\n"
            "Some topic body.\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.format() == content
        triple = next(b for b in file.blocks if isinstance(b, TripleHashBlock))
        assert triple.embedded_id == "2026-05-10 09:00 UTC"

    def test_round_trip_trailing_whitespace_preserved(self):
        content = "# Open Questions\n\nIntro paragraph.\n\n\n- [2026-05-12 20:18 UTC] q1\n"
        assert OpenQuestionsFile.parse(content).format() == content

    def test_known_question_ids_includes_triple_hash_embedded(self):
        content = (
            "# Open Questions\n"
            "\n"
            "### [2026-05-10 09:00 UTC] Topic: x\n"
            "narrative\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.known_question_ids == {
            "2026-05-10 09:00 UTC",
            "2026-05-12 20:18 UTC",
        }

    def test_open_ids_uses_position_not_resolved_by(self):
        """Bug #4: open-form entry physically under ## Resolved must report as resolved."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] still open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [2026-04-30 10:00 UTC] open-form-but-under-resolved\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["2026-05-12 20:18 UTC"]
        assert file.resolved_ids == ["2026-04-30 10:00 UTC"]

    def test_unparsable_block_appears_in_blocks(self):
        content = "# Open Questions\n\n- [2026-05-12 20:18 UTC] q1\n- [not-a-timestamp] garbage\n"
        file = OpenQuestionsFile.parse(content)
        kinds = [type(b).__name__ for b in file.blocks]
        assert "UnparsableBlock" in kinds


class TestUnresolvedEntries:
    def test_resolved_annotation_excluded_even_in_active_section(self):
        # A resolved-annotated entry sits interleaved before the divider; it is
        # excluded by annotation, unlike open_ids which keys on position.
        content = (
            "# Open Questions\n"
            "- [Q1] First open question?\n"
            "- [Resolved by D5 on 2026-04-02] [Q2] Already settled?\n"
            "- [Q3] Third open question?\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert [e.id for e in file.unresolved_entries] == ["Q1", "Q3"]
        # open_ids stays positional: with no divider it reports all three.
        assert file.open_ids == ["Q1", "Q2", "Q3"]

    def test_unannotated_entry_under_resolved_divider_is_unresolved(self):
        # The inverse of the open_ids Bug #4 case: an entry physically under
        # ## Resolved but lacking a resolution annotation is still unresolved by
        # annotation. open_ids treats it as resolved by position.
        content = (
            "# Open Questions\n"
            "- [Q1] Still open?\n"
            "## Resolved\n"
            "- [Q2] Open-form but under the divider?\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert [e.id for e in file.unresolved_entries] == ["Q1", "Q2"]
        assert file.open_ids == ["Q1"]


class TestResolve:
    def _file(self) -> OpenQuestionsFile:
        return OpenQuestionsFile.parse(
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1 body\n"
            "- [2026-05-11 15:29 UTC] q2 body\n"
        )

    def test_empty_ids_unchanged(self):
        file = self._file()
        result = file.resolve([], 139, date(2026, 5, 14))
        assert result.moved_ids == ()
        assert result.unknown_ids == ()
        assert result.file == file

    def test_resolve_single_entry(self):
        result = self._file().resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        assert result.moved_ids == ("2026-05-12 20:18 UTC",)
        assert result.unknown_ids == ()
        rendered = result.file.format()
        assert "- [Resolved by D139 on 2026-05-14] [2026-05-12 20:18 UTC] q1 body" in rendered
        assert "## Resolved" in rendered
        # Untouched entry still renders in open form.
        assert "- [2026-05-11 15:29 UTC] q2 body" in rendered

    def test_idempotent_when_already_resolved(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n"
            "\n"
            "- [2026-05-11 15:29 UTC] still open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        before = file.format()
        result = file.resolve(["2026-04-30 10:00 UTC"], 139, date(2026, 5, 14))
        assert result.moved_ids == ("2026-04-30 10:00 UTC",)
        assert result.unknown_ids == ()
        assert result.file.format() == before

    def test_unknown_id_surfaces(self):
        result = self._file().resolve(["2099-01-01 00:00 UTC"], 139, date(2026, 5, 14))
        assert result.moved_ids == ()
        assert result.unknown_ids == ("2099-01-01 00:00 UTC",)

    def test_mixed_move_idempotent_unknown(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        result = file.resolve(
            [
                "2026-05-12 20:18 UTC",
                "2026-04-30 10:00 UTC",
                "2099-01-01 00:00 UTC",
            ],
            139,
            date(2026, 5, 14),
        )
        assert set(result.moved_ids) == {
            "2026-05-12 20:18 UTC",
            "2026-04-30 10:00 UTC",
        }
        assert result.unknown_ids == ("2099-01-01 00:00 UTC",)
        rendered = result.file.format()
        assert "- [Resolved by D139 on 2026-05-14] [2026-05-12 20:18 UTC] q1" in rendered

    def test_continuation_lines_carry_over(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n\n- [2026-05-12 20:18 UTC] q1\n  Context: extra detail\n"
        )
        result = file.resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        rendered = result.file.format()
        assert "  Context: extra detail" in rendered
        # Continuation immediately follows the (now resolved) head line.
        head_idx = rendered.index("[Resolved by D139")
        assert rendered.index("Context: extra detail") > head_idx

    def test_dedupe_input_ids(self):
        result = self._file().resolve(
            ["2026-05-12 20:18 UTC", "2026-05-12 20:18 UTC"],
            139,
            date(2026, 5, 14),
        )
        assert result.moved_ids == ("2026-05-12 20:18 UTC",)

    def test_resolve_preserves_round_trip_for_unmodified_blocks(self):
        """Unmodified blocks (prose, unparsable, triple-hash, blanks) must survive resolve()."""
        content = (
            "# Open Questions\n"
            "\n"
            "### Topic: deployment\n"
            "narrative\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "\n"
            "Free-form prose between entries.\n"
            "\n"
            "- [not-a-timestamp] keep me\n"
            "- [2026-05-11 15:29 UTC] q2\n"
        )
        file = OpenQuestionsFile.parse(content)
        result = file.resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        rendered = result.file.format()
        assert "### Topic: deployment" in rendered
        assert "narrative" in rendered
        assert "Free-form prose between entries." in rendered
        assert "- [not-a-timestamp] keep me" in rendered
        assert "- [Resolved by D139 on 2026-05-14] [2026-05-12 20:18 UTC] q1" in rendered

    def test_resolve_with_no_existing_resolved_header_inserts_one(self):
        file = OpenQuestionsFile.parse("# Open Questions\n\n- [2026-05-12 20:18 UTC] q1\n")
        result = file.resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        rendered = result.file.format()
        assert rendered.count("## Resolved") == 1

    def test_resolve_with_existing_resolved_header_does_not_duplicate_it(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        result = file.resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        rendered = result.file.format()
        assert rendered.count("## Resolved") == 1

    def test_triple_hash_only_id_reported_unknown_not_moved(self):
        """An id that exists only inside a ``### [timestamp]`` block is not movable
        via resolve, so resolve reports it in unknown_ids even though
        known_question_ids includes it for boundary validation."""
        content = "# Open Questions\n\n### [2026-05-12 20:18 UTC] Topic question\n  body line\n"
        file = OpenQuestionsFile.parse(content)
        assert "2026-05-12 20:18 UTC" in file.known_question_ids
        result = file.resolve(["2026-05-12 20:18 UTC"], 200, date(2026, 5, 18))
        assert result.moved_ids == ()
        assert result.unknown_ids == ("2026-05-12 20:18 UTC",)
        assert result.file.format() == content

    def test_resolve_mutates_in_place_block_index_preserved(self):
        """Pin the in-place mutation contract: a resolved entry keeps its
        position in ``blocks``. ``resolve`` flips ``resolved_by`` on the
        matching EntryBlock without relocating it under ``## Resolved``.
        """
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] first open\n"
            "- [2026-05-11 15:29 UTC] second open\n"
            "- [2026-05-10 09:00 UTC] third open\n"
        )
        file = OpenQuestionsFile.parse(content)
        entry_ids_before = [b.entry.id for b in file.blocks if isinstance(b, EntryBlock)]
        result = file.resolve(["2026-05-11 15:29 UTC"], 139, date(2026, 5, 14))
        entry_ids_after = [b.entry.id for b in result.file.blocks if isinstance(b, EntryBlock)]
        # Block index of every entry stays where it was.
        assert entry_ids_before == entry_ids_after
        # The middle entry now carries a resolved_by ref; siblings stay open.
        flags = [
            b.entry.resolved_by is not None
            for b in result.file.blocks
            if isinstance(b, EntryBlock)
        ]
        assert flags == [False, True, False]
        # The resolved entry's rendered line stays inside the open section
        # of the file (before the appended divider), reflecting in-place mutation.
        rendered = result.file.format()
        resolved_idx = rendered.index("[Resolved by D139 on 2026-05-14]")
        divider_idx = rendered.index("## Resolved")
        assert resolved_idx < divider_idx


class TestEntryRender:
    def test_open_form(self):
        from datetime import datetime as _dt

        entry = QuestionEntry(
            timestamp=_dt(2026, 5, 12, 20, 18),
            body="q text",
        )
        assert entry.render() == ["- [2026-05-12 20:18 UTC] q text"]

    def test_resolved_form(self):
        from datetime import datetime as _dt

        entry = QuestionEntry(
            timestamp=_dt(2026, 5, 12, 20, 18),
            body="q text",
            resolved_by=ResolvedRef(decision_num=139, date=date(2026, 5, 14)),
        )
        assert entry.render() == [
            "- [Resolved by D139 on 2026-05-14] [2026-05-12 20:18 UTC] q text"
        ]


class TestBlockTypes:
    """Block type exports stay importable from nauro_core.questions."""

    def test_block_types_importable(self):
        assert HeaderBlock is not None
        assert ProseBlock is not None
        assert EntryBlock is not None
        assert TripleHashBlock is not None
        assert UnparsableBlock is not None


class TestQuestionEntryIdValidator:
    def test_requires_one_of_num_or_timestamp(self):
        with pytest.raises(ValidationError):
            QuestionEntry(body="missing both")

    def test_rejects_both_num_and_timestamp(self):
        with pytest.raises(ValidationError):
            QuestionEntry(num=1, timestamp=datetime(2026, 5, 19, 22, 13), body="both")

    def test_num_only_renders_q_form(self):
        entry = QuestionEntry(num=17, body="qbody")
        assert entry.id == "Q17"
        assert entry.render() == ["- [Q17] qbody"]

    def test_timestamp_only_renders_legacy_form(self):
        entry = QuestionEntry(timestamp=datetime(2026, 5, 19, 22, 13), body="qbody")
        assert entry.id == "2026-05-19 22:13 UTC"
        assert entry.render() == ["- [2026-05-19 22:13 UTC] qbody"]

    def test_resolved_prefix_keeps_q_form(self):
        entry = QuestionEntry(
            num=42,
            body="qbody",
            resolved_by=ResolvedRef(decision_num=7, date=date(2026, 5, 19)),
        )
        assert entry.render() == ["- [Resolved by D7 on 2026-05-19] [Q42] qbody"]


class TestParseQForm:
    def test_parse_q_form_open(self):
        content = "# Open Questions\n\n- [Q1] new id body\n"
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["Q1"]
        entry_blocks = [b for b in file.blocks if isinstance(b, EntryBlock)]
        assert entry_blocks[0].entry.num == 1
        assert entry_blocks[0].entry.timestamp is None

    def test_parse_q_form_resolved(self):
        content = "# Open Questions\n\n## Resolved\n\n- [Resolved by D7 on 2026-05-19] [Q3] body\n"
        file = OpenQuestionsFile.parse(content)
        assert file.resolved_ids == ["Q3"]
        entry_blocks = [b for b in file.blocks if isinstance(b, EntryBlock)]
        assert entry_blocks[0].entry.num == 3
        assert entry_blocks[0].entry.resolved_by == ResolvedRef(
            decision_num=7, date=date(2026, 5, 19)
        )

    def test_parse_q_form_round_trip_byte_identical(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] first\n"
            "- [Q17] seventeenth\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D7 on 2026-05-19] [Q2] resolved q\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_mixed_grammar_parse(self):
        """Both id forms parse from a single file."""
        content = (
            "# Open Questions\n\n- [Q1] new-form open\n- [2026-05-12 20:18 UTC] legacy-form open\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["Q1", "2026-05-12 20:18 UTC"]

    def test_mixed_grammar_round_trip_byte_identical(self):
        """Any input not touched by resolve must round-trip verbatim."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q5] new-form open\n"
            "- [2026-05-12 20:18 UTC] legacy-form open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D11 on 2026-05-14] [Q2] new-form resolved\n"
            "- [Resolved by D12 on 2026-05-01] [2026-04-30 10:00 UTC] legacy-form resolved\n"
        )
        assert OpenQuestionsFile.parse(content).format() == content

    def test_known_question_ids_includes_both_forms(self):
        content = "# Open Questions\n\n- [Q1] new\n- [2026-05-12 20:18 UTC] legacy\n"
        file = OpenQuestionsFile.parse(content)
        assert file.known_question_ids == {"Q1", "2026-05-12 20:18 UTC"}

    def test_extract_embedded_id_accepts_q_form(self):
        """### topic heads may embed Q-form ids; boundary validation needs them."""
        content = "# Open Questions\n\n### [Q9] Topic with Q-form id\nnarrative\n"
        file = OpenQuestionsFile.parse(content)
        triple = next(b for b in file.blocks if isinstance(b, TripleHashBlock))
        assert triple.embedded_id == "Q9"
        assert "Q9" in file.known_question_ids

    def test_parse_q_zero_degrades_to_unparsable_block(self):
        """`[Q0]` must not raise: ``num`` carries ``ge=1`` and a Q-id of 0
        would crash the whole file. The parser treats it as malformed
        Q-grammar and lands the line in UnparsableBlock, preserving the
        round-trip-for-unmodified-blocks contract."""
        content = "# Open Questions\n\n- [Q0] body\n"
        file = OpenQuestionsFile.parse(content)
        kinds = [type(b).__name__ for b in file.blocks]
        assert "UnparsableBlock" in kinds
        unparsable = next(b for b in file.blocks if isinstance(b, UnparsableBlock))
        assert unparsable.lines == ("- [Q0] body",)

    def test_parse_q_zero_round_trip_byte_identical(self):
        content = "# Open Questions\n\n- [Q0] body\n"
        assert OpenQuestionsFile.parse(content).format() == content

    @pytest.mark.parametrize(
        "head",
        ["[Q-3]", "[Q]", "[Qabc]", "[Q1.5]", "[Q 5]"],
    )
    def test_parse_malformed_q_grammar_falls_through_to_unparsable(self, head):
        """Anything that's not a strictly-positive integer after ``Q`` is
        not a Q-id. The strptime fallback also fails, so the line lands
        in UnparsableBlock and the file round-trips verbatim."""
        content = f"# Open Questions\n\n- {head} body\n"
        file = OpenQuestionsFile.parse(content)
        kinds = [type(b).__name__ for b in file.blocks]
        assert "UnparsableBlock" in kinds
        assert file.format() == content


class TestResolveQForm:
    def test_resolve_by_q_id(self):
        file = OpenQuestionsFile.parse("# Open Questions\n\n- [Q1] body\n")
        result = file.resolve(["Q1"], 7, date(2026, 5, 19))
        assert result.moved_ids == ("Q1",)
        assert result.unknown_ids == ()
        assert result.ambiguous_ids == ()
        rendered = result.file.format()
        assert "- [Resolved by D7 on 2026-05-19] [Q1] body" in rendered

    def test_resolve_legacy_id_still_works(self):
        """Dual-grammar acceptance: legacy timestamp ids must keep resolving."""
        file = OpenQuestionsFile.parse("# Open Questions\n\n- [2026-05-12 20:18 UTC] legacy\n")
        result = file.resolve(["2026-05-12 20:18 UTC"], 7, date(2026, 5, 19))
        assert result.moved_ids == ("2026-05-12 20:18 UTC",)
        assert result.ambiguous_ids == ()

    def test_resolve_mixed_request(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n\n- [Q1] new\n- [2026-05-12 20:18 UTC] legacy\n"
        )
        result = file.resolve(["Q1", "2026-05-12 20:18 UTC"], 7, date(2026, 5, 19))
        assert set(result.moved_ids) == {"Q1", "2026-05-12 20:18 UTC"}


class TestResolveRejectsAmbiguous:
    """Same-minute timestamp collision must not silently move both
    entries under one back-reference."""

    def _collision_file(self) -> OpenQuestionsFile:
        # Two legacy entries sharing the same id — the production bug shape.
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] first colliding question\n"
            "- [2026-05-12 20:18 UTC] second colliding question\n"
        )
        return OpenQuestionsFile.parse(content)

    def test_ambiguous_ids_property_reports_collision(self):
        assert self._collision_file().ambiguous_ids == {"2026-05-12 20:18 UTC": 2}

    def test_ambiguous_ids_empty_when_unique(self):
        file = OpenQuestionsFile.parse("# Open Questions\n\n- [Q1] one\n- [Q2] two\n")
        assert file.ambiguous_ids == {}

    def test_resolve_rejects_ambiguous_without_mutation(self):
        file = self._collision_file()
        before = file.format()
        result = file.resolve(["2026-05-12 20:18 UTC"], 7, date(2026, 5, 19))
        assert result.ambiguous_ids == ("2026-05-12 20:18 UTC",)
        assert result.moved_ids == ()
        assert result.unknown_ids == ()
        # No mutation: identical file returned.
        assert result.file.format() == before

    def test_resolve_returns_unchanged_file_when_one_id_ambiguous(self):
        """Even a partial-ambiguity request rejects entirely — defense in
        depth means we never partial-mutate when ambiguity is detected."""
        file = OpenQuestionsFile.parse(
            "# Open Questions\n"
            "\n"
            "- [Q1] unambiguous\n"
            "- [2026-05-12 20:18 UTC] dup\n"
            "- [2026-05-12 20:18 UTC] dup again\n"
        )
        before = file.format()
        result = file.resolve(["Q1", "2026-05-12 20:18 UTC"], 7, date(2026, 5, 19))
        assert result.ambiguous_ids == ("2026-05-12 20:18 UTC",)
        assert result.moved_ids == ()
        assert result.file.format() == before


class TestMigrate:
    """Legacy ``[timestamp]`` -> sequential ``Q###`` migration."""

    def test_mints_sequential_ids_and_appends_logged_timestamp(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] first legacy q\n"
            "- [2026-05-11 15:29 UTC] second legacy q\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        assert result.file.format() == (
            "# Open Questions\n"
            "\n"
            "- [Q1] first legacy q (logged 2026-05-12 20:18 UTC)\n"
            "- [Q2] second legacy q (logged 2026-05-11 15:29 UTC)\n"
        )
        assert result.renames == (
            MigrationRename(
                old_id="2026-05-12 20:18 UTC",
                new_id="Q1",
                logged="(logged 2026-05-12 20:18 UTC)",
            ),
            MigrationRename(
                old_id="2026-05-11 15:29 UTC",
                new_id="Q2",
                logged="(logged 2026-05-11 15:29 UTC)",
            ),
        )

    def test_continues_past_existing_q_ids(self):
        """The next id is max(num) + 1 across the whole file, not 1."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q5] already migrated\n"
            "- [2026-05-12 20:18 UTC] legacy after a high Q\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        assert result.renames == (
            MigrationRename(
                old_id="2026-05-12 20:18 UTC",
                new_id="Q6",
                logged="(logged 2026-05-12 20:18 UTC)",
            ),
        )
        assert "- [Q5] already migrated\n" in result.file.format()
        assert "- [Q6] legacy after a high Q (logged 2026-05-12 20:18 UTC)\n" in (
            result.file.format()
        )

    def test_resolved_legacy_entry_keeps_prefix_rewrites_id(self):
        """A resolved legacy entry keeps its [Resolved by ...] prefix; only
        the id segment becomes [Q###]."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] still open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        out = result.file.format()
        assert "- [Q1] still open (logged 2026-05-12 20:18 UTC)" in out
        assert "- [Resolved by D100 on 2026-05-01] [Q2] q-old (logged 2026-04-30 10:00 UTC)" in out

    def test_resolved_entry_stays_under_resolved_divider(self):
        """No relocation: the resolved entry keeps its position after the
        ## Resolved divider with its id rewritten."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] open\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        assert result.file.open_ids == ["Q1"]
        assert result.file.resolved_ids == ["Q2"]

    def test_idempotent_on_all_q_form(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] one\n"
            "- [Q2] two\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [Q3] three\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        assert result.renames == ()
        assert result.file.format() == content

    def test_non_legacy_blocks_are_byte_identical(self):
        """Prose, ### blocks, headers, the divider, unparsable lines, and
        already-Q entries round-trip verbatim through migrate."""
        content = (
            "# Open Questions\n"
            "\n"
            "Free-form intro a human wrote.\n"
            "\n"
            "### [2026-05-10 09:00 UTC] Topic: deployment\n"
            "Topic narrative kept verbatim.\n"
            "\n"
            "- [Q4] already migrated\n"
            "- [not-a-timestamp] garbage but please keep\n"
            "- [2026-05-12 20:18 UTC] the one legacy entry\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        out = result.file.format()
        # Only the single legacy entry changed.
        assert "- [Q5] the one legacy entry (logged 2026-05-12 20:18 UTC)" in out
        # Everything else is verbatim.
        for verbatim in (
            "Free-form intro a human wrote.",
            "### [2026-05-10 09:00 UTC] Topic: deployment",
            "Topic narrative kept verbatim.",
            "- [Q4] already migrated",
            "- [not-a-timestamp] garbage but please keep",
        ):
            assert verbatim in out

    def test_migrated_output_reparses_stably(self):
        """Re-parsing migrated output and re-formatting is a fixed point."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "  Context: a continuation line\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D100 on 2026-05-01] [2026-04-30 10:00 UTC] q-old\n"
        )
        once = OpenQuestionsFile.parse(content).migrate().file.format()
        twice = OpenQuestionsFile.parse(once).format()
        assert once == twice
        # And a second migrate over the migrated output is a no-op.
        second = OpenQuestionsFile.parse(once).migrate()
        assert second.renames == ()
        assert second.file.format() == once

    def test_continuation_lines_survive(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] q1\n"
            "  Context: extra detail\n"
            "  Another line\n"
        )
        result = OpenQuestionsFile.parse(content).migrate()
        assert result.file.format() == (
            "# Open Questions\n"
            "\n"
            "- [Q1] q1 (logged 2026-05-12 20:18 UTC)\n"
            "  Context: extra detail\n"
            "  Another line\n"
        )

    def test_empty_file_is_noop(self):
        result = OpenQuestionsFile.parse("").migrate()
        assert result.renames == ()
        assert result.file.format() == "# Open Questions"

    def test_same_minute_collision_gets_distinct_ids(self):
        """Two legacy entries sharing one timestamp id — the collision this
        migration exists to fix — mint two distinct Q ids and stop being
        ambiguous."""
        content = (
            "# Open Questions\n"
            "\n"
            "- [2026-05-12 20:18 UTC] first colliding question\n"
            "- [2026-05-12 20:18 UTC] second colliding question\n"
        )
        parsed = OpenQuestionsFile.parse(content)
        assert parsed.ambiguous_ids == {"2026-05-12 20:18 UTC": 2}

        result = parsed.migrate()
        new_ids = [r.new_id for r in result.renames]
        assert new_ids == ["Q1", "Q2"]
        assert len(set(new_ids)) == 2
        # Both records carry the shared legacy id.
        assert [r.old_id for r in result.renames] == [
            "2026-05-12 20:18 UTC",
            "2026-05-12 20:18 UTC",
        ]
        # The collision is gone after migration.
        assert result.file.ambiguous_ids == {}
