"""Tests for nauro_core.questions."""

from datetime import date

import pytest

from nauro_core.questions import (
    OpenQuestionsFile,
    QuestionEntry,
    ResolvedRef,
)


class TestResolvedRefValidation:
    def test_decision_num_must_be_positive(self):
        with pytest.raises(ValueError):
            ResolvedRef(decision_num=0, date=date(2026, 5, 14))


class TestParseRoundTrip:
    def test_empty_file_yields_default_header(self):
        file = OpenQuestionsFile.parse("")
        assert file.header == "# Open Questions"
        assert file.entries == []

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
        resolved_entry = file.entries[1]
        assert resolved_entry.resolved_by == ResolvedRef(decision_num=100, date=date(2026, 5, 1))

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
        assert file.entries[0].continuation == [
            "  Context: extra detail",
            "  Another continuation line",
        ]
        assert file.entries[1].continuation == []

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
        # Re-parsing the rendered output yields the same model.
        re_parsed = OpenQuestionsFile.parse(rendered)
        assert re_parsed == file

    def test_malformed_timestamp_skipped(self):
        content = (
            "# Open Questions\n\n- [not-a-timestamp] junk line\n- [2026-05-12 20:18 UTC] valid q\n"
        )
        file = OpenQuestionsFile.parse(content)
        assert file.open_ids == ["2026-05-12 20:18 UTC"]


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
        assert result.file.open_ids == ["2026-05-11 15:29 UTC"]
        assert result.file.resolved_ids == ["2026-05-12 20:18 UTC"]
        rendered = result.file.format()
        assert "- [Resolved by D139 on 2026-05-14] [2026-05-12 20:18 UTC] q1 body" in rendered

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
        # Already-resolved is not rewritten — rendered output is unchanged.
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
        assert result.file.open_ids == []

    def test_continuation_lines_carry_over(self):
        file = OpenQuestionsFile.parse(
            "# Open Questions\n\n- [2026-05-12 20:18 UTC] q1\n  Context: extra detail\n"
        )
        result = file.resolve(["2026-05-12 20:18 UTC"], 139, date(2026, 5, 14))
        rendered = result.file.format()
        assert "  Context: extra detail" in rendered
        # Continuation appears under the resolved heading, not before it.
        head_idx = rendered.index("## Resolved")
        assert rendered.index("Context: extra detail") > head_idx

    def test_dedupe_input_ids(self):
        result = self._file().resolve(
            ["2026-05-12 20:18 UTC", "2026-05-12 20:18 UTC"],
            139,
            date(2026, 5, 14),
        )
        assert result.moved_ids == ("2026-05-12 20:18 UTC",)


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
