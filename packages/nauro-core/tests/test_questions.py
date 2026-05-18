"""Tests for nauro_core.questions."""

from datetime import date

import pytest

from nauro_core.questions import (
    EntryBlock,
    HeaderBlock,
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
