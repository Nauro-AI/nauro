"""Tests for nauro_core.context — L0/L1/L2 context assembly."""

from datetime import date as _date
from datetime import datetime, timedelta, timezone

from nauro_core.constants import (
    L0_QUESTIONS_LIMIT,
    PROJECT_MD_SCAFFOLD_BODY,
    QUESTION_ENTRY_CHAR_BUDGET,
    STACK_AUTO_CHAR_BUDGET,
    STACK_AUTO_ITEM_CHAR_LIMIT,
    STACK_NON_AUTHORITATIVE_FRAMING,
)
from nauro_core.context import build_l0, build_l1, build_l2
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
)
from nauro_core.questions import QUESTION_TRUNCATION_POINTER


def _make_decision(num, title, status="active", date="2026-04-01", rationale="Reason."):
    status_enum = DecisionStatus(status)
    content = f"# {num:03d} \u2014 {title}\n\nstatus: {status}\n\n## Decision\n\n{rationale}\n"
    return Decision(
        date=_date.fromisoformat(date),
        confidence=DecisionConfidence.medium,
        status=status_enum,
        superseded_by="999" if status_enum is DecisionStatus.superseded else None,
        num=num,
        title=title,
        rationale=rationale,
        content=content,
    )


FULL_FILES = {
    "project.md": "# MyProject\nGoal: build something great.",
    "state.md": (
        "# State\n\n"
        "## Current\n"
        "Shipping v1 — finishing auth module\n\n"
        "## History\n"
        "- **2026-03-01:** Set up project scaffolding\n"
    ),
    "stack.md": (
        "# Stack\n"
        "## Language\n"
        "- **Python 3.11+** \u2014 main language\n"
        "  - Chose over Go for ecosystem\n"
        "## Infrastructure\n"
        "- **AWS Lambda** \u2014 serverless\n"
    ),
    "open-questions.md": (
        "# Open Questions\n"
        "- [Q1] How does auth work?\n"
        "- [Q2] What about caching?\n"
        "- [Q3] Redis or Memcached?\n"
        "- [Q4] Deploy strategy?\n"
        "- [Q5] Monitoring setup?\n"
        "- [Q6] Sixth question?\n"
    ),
}

DECISIONS = [
    _make_decision(1, "Use FastAPI", date="2026-03-01"),
    _make_decision(2, "Choose S3", date="2026-03-15"),
    _make_decision(3, "Auth0 for OAuth", date="2026-03-20"),
    _make_decision(4, "Old choice", status="superseded", date="2026-02-01"),
]


class TestBuildL0:
    def test_full_files(self):
        result = build_l0(FULL_FILES, DECISIONS)
        # The project body leads the payload; its H1 is stripped.
        assert "Goal: build something great." in result
        assert "# MyProject" not in result
        assert "## Current State" in result
        assert "Shipping v1" in result
        assert "**Stack:** Python 3.11+" in result
        assert "## Open Questions" in result
        assert "How does auth work?" in result
        assert "## Recent Decisions" in result

    def test_current_state_only(self):
        result = build_l0(FULL_FILES, DECISIONS)
        assert "Shipping v1" in result
        assert "Set up project scaffolding" not in result  # history excluded

    def test_current_state_header_not_doubled(self):
        # state_current.md carries its own "# Current State" header; L0 wraps it
        # under a "## Current State" section header. The two must not stutter.
        files = {"state_current.md": "# Current State\n\n- Shipped the thing\n"}
        result = build_l0(files, [])
        assert "## Current State" in result
        assert "# Current State\n# Current State" not in result
        assert "## Current State\n# Current State" not in result
        assert "- Shipped the thing" in result

    def test_decisions_summary_present(self):
        result = build_l0(FULL_FILES, DECISIONS)
        assert "D3 \u2014 Auth0 for OAuth" in result
        assert "D2 \u2014 Choose S3" in result
        assert "D1 \u2014 Use FastAPI" in result

    def test_superseded_excluded_from_summary(self):
        result = build_l0(FULL_FILES, DECISIONS)
        assert "Old choice" not in result

    def test_questions_limit_3(self):
        result = build_l0(FULL_FILES, DECISIONS)
        assert "Sixth question?" not in result
        assert "Deploy strategy?" not in result
        assert "Monitoring setup?" not in result
        assert "Redis or Memcached?" in result

    def test_missing_project_md(self):
        files = {k: v for k, v in FULL_FILES.items() if k != "project.md"}
        result = build_l0(files, DECISIONS)
        assert "# MyProject" not in result
        assert "Shipping v1" in result

    def test_empty_decisions(self):
        result = build_l0(FULL_FILES, [])
        assert "## Recent Decisions" not in result
        assert "Goal: build something great." in result
        assert "# MyProject" not in result

    def test_empty_files(self):
        result = build_l0({}, [])
        assert result == ""

    def test_stack_oneliner(self):
        result = build_l0(FULL_FILES, [])
        assert "**Stack:** Python 3.11+, AWS Lambda" in result
        assert "Chose over Go" not in result
        assert "## Language" not in result

    def test_state_current_md_key(self):
        files = {**FULL_FILES, "state_current.md": "# Current State\n\nNew format state"}
        del files["state.md"]
        result = build_l0(files, [])
        assert "New format state" in result
        assert "Set up project scaffolding" not in result

    def test_state_md_fallback(self):
        # Only state.md present (pre-upgrade store) — should extract ## Current
        result = build_l0(FULL_FILES, [])
        assert "Shipping v1" in result
        assert "Set up project scaffolding" not in result

    def test_state_current_wins_over_legacy(self):
        files = {
            **FULL_FILES,
            "state_current.md": "# Current State\n\nNew format wins",
        }
        result = build_l0(files, [])
        assert "New format wins" in result
        assert "Shipping v1" not in result


class TestBuildL0ProjectPreamble:
    """L0 leads with the project.md body as a stable-scope preamble.

    The leading H1 is stripped (the consuming surface supplies the title
    heading), and content still in unedited ``nauro init`` scaffold form is
    skipped entirely. L1/L2 are untouched: they carry project.md verbatim.
    """

    SCAFFOLD = "# my-proj\n" + PROJECT_MD_SCAFFOLD_BODY

    def test_project_body_is_first_section(self):
        result = build_l0(FULL_FILES, DECISIONS)
        assert result.startswith("Goal: build something great.")
        assert result.index("Goal: build something great.") < result.index("## Current State")

    def test_scaffold_form_project_md_skipped(self):
        files = {**FULL_FILES, "project.md": self.SCAFFOLD}
        result = build_l0(files, DECISIONS)
        assert "**One-liner:**" not in result
        assert "my-proj" not in result
        assert result.startswith("## Current State")

    def test_h1_strip_handles_leading_blank_lines(self):
        files = {"project.md": "\n\n# MyProject\n\nGoal: something.\n"}
        result = build_l0(files, [])
        assert result == "Goal: something."

    def test_crlf_project_md_renders_preamble_without_carriage_returns(self):
        files = {"project.md": "# MyProject\r\n\r\nGoal: something.\r\n## Goals\r\n- ship it\r\n"}
        result = build_l0(files, [])
        assert "\r" not in result
        assert result.startswith("Goal: something.")
        assert "## Goals" in result

    def test_no_h1_file_passes_through(self):
        files = {"project.md": "Goal without heading.\n## Goals\n- ship it\n"}
        result = build_l0(files, [])
        assert result.startswith("Goal without heading.")
        assert "## Goals" in result

    def test_h1_only_project_md_renders_nothing(self):
        files = {"project.md": "# MyProject\n"}
        result = build_l0(files, [])
        assert result == ""

    def test_l1_includes_scaffold_form_project_md_verbatim(self):
        files = {**FULL_FILES, "project.md": self.SCAFFOLD}
        result = build_l1(files, DECISIONS)
        assert "# my-proj" in result
        assert "**One-liner:**" in result

    def test_l2_includes_scaffold_form_project_md_verbatim(self):
        files = {**FULL_FILES, "project.md": self.SCAFFOLD}
        result = build_l2(files, DECISIONS)
        assert "# my-proj" in result
        assert "**One-liner:**" in result


class TestBuildL1:
    def test_canonical_ordering(self):
        result = build_l1(FULL_FILES, DECISIONS)
        project_pos = result.find("# MyProject")
        state_pos = result.find("# State")
        stack_pos = result.find("# Stack")
        questions_pos = result.find("# Open Questions")
        decisions_pos = result.find("## Decisions")
        assert project_pos < state_pos < stack_pos < questions_pos < decisions_pos

    def test_stack_is_bounded_projection(self):
        # Intentional contract change: L1 carries the bounded stack
        # projection, not the verbatim file. Headings and first-level list
        # items render; the nested rationale line drops out; the framing
        # line opens the block.
        result = build_l1(FULL_FILES, DECISIONS)
        assert "# Stack" in result
        assert "- **Python 3.11+** — main language" in result
        assert "- **AWS Lambda** — serverless" in result
        assert "Chose over Go for ecosystem" not in result
        assert STACK_NON_AUTHORITATIVE_FRAMING in result

    def test_questions_projection_renders_all_under_cap(self):
        # Six genuine open entries against a cap of 10: the projection
        # renders every entry and appends no omission trailer.
        result = build_l1(FULL_FILES, DECISIONS)
        assert "Sixth question?" in result
        assert "more open questions" not in result

    def test_full_decision_content(self):
        result = build_l1(FULL_FILES, DECISIONS)
        assert "## Decision" in result
        assert "Reason." in result

    def test_superseded_excluded(self):
        result = build_l1(FULL_FILES, DECISIONS)
        assert "Old choice" not in result

    def test_missing_project_md(self):
        files = {k: v for k, v in FULL_FILES.items() if k != "project.md"}
        result = build_l1(files, DECISIONS)
        assert "# MyProject" not in result
        assert "## Decisions" in result

    def test_earlier_decisions_with_many(self):
        decisions = [_make_decision(i, f"Decision {i}") for i in range(1, 25)]
        result = build_l1(FULL_FILES, decisions)
        assert "## Earlier Decisions" in result

    def test_no_earlier_when_few(self):
        result = build_l1(FULL_FILES, DECISIONS)
        assert "## Earlier Decisions" not in result

    def test_empty_decisions(self):
        result = build_l1(FULL_FILES, [])
        assert "## Decisions" not in result

    def test_state_current_md_key(self):
        files = {**FULL_FILES, "state_current.md": "# Current State\n\nNew format L1"}
        del files["state.md"]
        result = build_l1(files, [])
        assert "New format L1" in result

    def test_state_md_fallback(self):
        result = build_l1(FULL_FILES, [])
        assert "# State" in result


class TestBuildL2:
    def test_all_decisions_included(self):
        result = build_l2(FULL_FILES, DECISIONS)
        assert "## All Decisions" in result
        assert "Use FastAPI" in result
        assert "Old choice" in result  # superseded included in L2

    def test_questions_included(self):
        result = build_l2(FULL_FILES, DECISIONS)
        assert "Open Questions" in result

    def test_empty_decisions(self):
        result = build_l2(FULL_FILES, [])
        assert "## All Decisions" not in result

    def test_empty_everything(self):
        result = build_l2({}, [])
        assert result == ""

    def test_decisions_separated_by_hr(self):
        result = build_l2({}, DECISIONS)
        assert "\n\n---\n\n" in result

    def test_includes_state_history(self):
        files = {
            "state_current.md": "# Current State\n\nCurrent stuff",
            "state_history.md": "## 2026-04-01T10:00Z\n\nOld stuff\n\n---\n",
        }
        result = build_l2(files, [])
        assert "Current stuff" in result
        assert "# State History" in result
        assert "Old stuff" in result

    def test_state_current_without_history(self):
        files = {"state_current.md": "# Current State\n\nJust current"}
        result = build_l2(files, [])
        assert "Just current" in result
        assert "# State History" not in result

    def test_state_md_fallback_l2(self):
        result = build_l2(FULL_FILES, [])
        assert "# State" in result

    def test_project_included(self):
        # The full dump must carry project.md; omitting it previously made L2
        # both incomplete and smaller than L1.
        result = build_l2(FULL_FILES, DECISIONS)
        assert "# MyProject" in result
        assert "build something great" in result

    def test_stack_included(self):
        result = build_l2(FULL_FILES, DECISIONS)
        assert "# Stack" in result
        assert "Chose over Go for ecosystem" in result

    def test_canonical_ordering(self):
        result = build_l2(FULL_FILES, DECISIONS)
        project_pos = result.find("# MyProject")
        state_pos = result.find("# State")
        stack_pos = result.find("# Stack")
        questions_pos = result.find("# Open Questions")
        decisions_pos = result.find("## All Decisions")
        assert project_pos < state_pos < stack_pos < questions_pos < decisions_pos

    def test_superset_of_l1(self):
        # Every project/stack/questions string L1 surfaces must also appear in
        # the full dump, and L2 must additionally carry superseded decisions
        # and the nested stack rationale L1's bounded projection drops.
        # L0/L1 omission trailers and age-nudge lines are projection annotations,
        # not store content, so they are absent from L2.
        l1 = build_l1(FULL_FILES, DECISIONS)
        l2 = build_l2(FULL_FILES, DECISIONS)
        for marker in (
            "# MyProject",
            "- **Python 3.11+** — main language",
            "Sixth question?",
            STACK_NON_AUTHORITATIVE_FRAMING,
        ):
            assert marker in l1 and marker in l2
        assert "Old choice" in l2 and "Old choice" not in l1
        assert "Chose over Go for ecosystem" in l2
        assert "Chose over Go for ecosystem" not in l1

    def test_missing_project_md(self):
        files = {k: v for k, v in FULL_FILES.items() if k != "project.md"}
        result = build_l2(files, DECISIONS)
        assert "# MyProject" not in result
        assert "## All Decisions" in result


def _legacy_id(target_date) -> str:
    """Render a legacy ``[YYYY-MM-DD HH:MM UTC]`` id for ``target_date``."""
    return target_date.strftime("%Y-%m-%d") + " 12:00 UTC"


class TestBuildL0OpenQuestionsAgeProjection:
    """L0 prepends an age hint above open questions older than 30 days.

    Walks ``OpenQuestionsFile.parse(...).blocks`` so the entry's
    ``timestamp`` is available for the age computation. Q-form entries
    without a minted-at timestamp render without the projection.

    Tests pin the threshold by sourcing ``today`` from the same
    ``datetime.now(timezone.utc).date()`` the builder uses, so the
    boundary assertions stay deterministic regardless of wall-clock
    timezone offsets.
    """

    _PROJECTION_PREFIX = "(open"

    def _files(self, content: str) -> dict[str, str]:
        return {"open-questions.md": content}

    def _today(self):
        return datetime.now(timezone.utc).date()

    def test_legacy_entry_31_days_old_renders_projection(self):
        target = self._today() - timedelta(days=31)
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] Old question?\n"
        result = build_l0(self._files(content), [])
        assert "## Open Questions" in result
        assert "(open 31 days; consider closing or deferring)" in result
        assert "Old question?" in result

    def test_legacy_entry_29_days_old_skips_projection(self):
        target = self._today() - timedelta(days=29)
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] Fresh question?\n"
        result = build_l0(self._files(content), [])
        assert "## Open Questions" in result
        assert "Fresh question?" in result
        assert self._PROJECTION_PREFIX not in result

    def test_legacy_entry_30_days_old_skips_projection(self):
        # 30-day boundary: projection fires only on > 30 days (strictly older).
        # Exactly-30 stays clean so the threshold is not noisily reached on
        # the first day a question crosses month-old territory.
        target = self._today() - timedelta(days=30)
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] Right on the line?\n"
        result = build_l0(self._files(content), [])
        assert "Right on the line?" in result
        assert self._PROJECTION_PREFIX not in result

    def test_q_form_entry_renders_without_projection(self):
        # Q-form entries do not carry a minted-at timestamp today, so the
        # projection skips them regardless of age. The gap closes when
        # flag_question stamps minted-at on Q-form entries (out of scope).
        content = "# Open Questions\n\n- [Q5] Q-form has no timestamp.\n"
        result = build_l0(self._files(content), [])
        assert "Q-form has no timestamp." in result
        assert self._PROJECTION_PREFIX not in result

    def test_only_first_three_open_entries_render(self):
        # Honours L0_QUESTIONS_LIMIT (3). The fourth open entry is omitted
        # even when older than 30 days; the projection doesn't expand the cap.
        today = self._today()
        content = (
            "# Open Questions\n"
            "\n"
            f"- [{_legacy_id(today - timedelta(days=40))}] first?\n"
            f"- [{_legacy_id(today - timedelta(days=35))}] second?\n"
            f"- [{_legacy_id(today - timedelta(days=33))}] third?\n"
            f"- [{_legacy_id(today - timedelta(days=60))}] fourth?\n"
        )
        result = build_l0(self._files(content), [])
        assert "first?" in result
        assert "second?" in result
        assert "third?" in result
        assert "fourth?" not in result
        assert (
            '(+1 more open questions — see get_raw_file("open-questions.md") '
            "for the full file, including resolved history)"
        ) in result

    def test_resolved_entries_skipped_in_l0(self):
        # Entries physically under ## Resolved (position-based partition)
        # are not L0 surface. They must not render even when fresh.
        today = self._today()
        old_id = _legacy_id(today - timedelta(days=60))
        content = (
            "# Open Questions\n"
            "\n"
            f"- [{_legacy_id(today - timedelta(days=2))}] still open?\n"
            "\n"
            "## Resolved\n"
            "\n"
            f"- [Resolved by D42 on 2026-05-01] [{old_id}] resolved old?\n"
        )
        result = build_l0(self._files(content), [])
        assert "still open?" in result
        assert "resolved old?" not in result

    def test_empty_questions_file_renders_nothing(self):
        result = build_l0(self._files(""), [])
        assert "## Open Questions" not in result

    def test_projection_appears_above_its_entry(self):
        # Pin layout: the projection line precedes the entry it nudges,
        # not below it, so the hint is read before the question body.
        target = self._today() - timedelta(days=45)
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] Stale question?\n"
        result = build_l0(self._files(content), [])
        proj_idx = result.index("(open 45 days")
        entry_idx = result.index("Stale question?")
        assert proj_idx < entry_idx

    def test_mixed_ages_only_old_get_projection(self):
        today = self._today()
        content = (
            "# Open Questions\n"
            "\n"
            f"- [{_legacy_id(today - timedelta(days=45))}] Old one?\n"
            f"- [{_legacy_id(today - timedelta(days=5))}] Recent one?\n"
        )
        result = build_l0(self._files(content), [])
        # Old one carries a projection; recent one does not.
        assert "(open 45 days" in result
        assert "Old one?" in result
        assert "Recent one?" in result
        # No projection lurks between the old entry and the recent one.
        recent_idx = result.index("Recent one?")
        old_idx = result.index("Old one?")
        between = result[old_idx + len("Old one?") : recent_idx]
        assert self._PROJECTION_PREFIX not in between


class TestBuildL0DiscoveryPointerExclusion:
    """Discovery-pointer entries (BRIEF:/RESUME:/SELECT: body prefix) are
    excluded from the L0 Open Questions section and do not consume a limit slot.
    """

    def _files(self, content: str) -> dict[str, str]:
        return {"open-questions.md": content}

    def test_brief_pointer_excluded(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] BRIEF: context/topic-20260601-ab12.md — shared brief\n"
            "- [Q2] Genuine open question?\n"
        )
        result = build_l0(self._files(content), [])
        assert "Genuine open question?" in result
        assert "BRIEF:" not in result

    def test_resume_pointer_excluded(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] RESUME: context/auth-cutover-20260601-cd34.md — resume brief\n"
            "- [Q2] Another genuine question?\n"
        )
        result = build_l0(self._files(content), [])
        assert "Another genuine question?" in result
        assert "RESUME:" not in result

    def test_select_pointer_excluded(self):
        # SELECT: checkpoints (nauro-loop candidate sets) are discovery
        # pointers too and must not surface as L0 open questions.
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] SELECT: context/origin-select-20260619-ef56.md — loop checkpoint\n"
            "- [Q2] Yet another genuine question?\n"
        )
        result = build_l0(self._files(content), [])
        assert "Yet another genuine question?" in result
        assert "SELECT:" not in result

    def test_pointer_does_not_consume_limit_slot(self):
        # The three pointers consume no slots, and the trailer counts only the
        # fourth genuine question omitted beyond the cap.
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] BRIEF: context/a.md — pointer one\n"
            "- [Q2] First genuine question?\n"
            "- [Q3] Second genuine question?\n"
            "- [Q4] RESUME: context/b.md — pointer two\n"
            "- [Q5] Third genuine question?\n"
            "- [Q6] Fourth genuine question — should be cut by limit?\n"
            "- [Q7] SELECT: context/c.md — pointer three\n"
        )
        result = build_l0(self._files(content), [])
        assert "First genuine question?" in result
        assert "Second genuine question?" in result
        assert "Third genuine question?" in result
        assert "Fourth genuine question" not in result
        assert "BRIEF:" not in result
        assert "RESUME:" not in result
        assert "SELECT:" not in result
        assert (
            '(+1 more open questions — see get_raw_file("open-questions.md") '
            "for the full file, including resolved history)"
        ) in result

    def test_pointer_free_file_unaffected(self):
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] How does auth work?\n"
            "- [Q2] What about caching?\n"
            "- [Q3] Redis or Memcached?\n"
        )
        result = build_l0(self._files(content), [])
        assert "How does auth work?" in result
        assert "What about caching?" in result
        assert "Redis or Memcached?" in result
        assert "## Open Questions" in result

    def test_all_pointers_yields_empty_open_questions(self):
        # When every open entry is a pointer, the section is suppressed entirely.
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] BRIEF: context/x.md — brief one\n"
            "- [Q2] RESUME: context/y.md — resume one\n"
        )
        result = build_l0(self._files(content), [])
        assert "## Open Questions" not in result


class TestBuildL1OpenQuestionsProjection:
    """L1 renders a capped projection of genuine open questions.

    The section carries the first ``L1_QUESTIONS_LIMIT`` genuine open
    entries in file order. Resolved history (both the ``## Resolved``
    section and ``resolved_by``-annotated entries above the divider) and
    discovery pointers are excluded and never consume a slot. When genuine
    entries are omitted beyond the cap, a single trailer line reports the
    count and points at ``get_raw_file`` for the full file.
    """

    _TRAILER_2 = (
        '(+2 more open questions — see get_raw_file("open-questions.md") '
        "for the full file, including resolved history)"
    )

    def _files(self, content: str) -> dict[str, str]:
        return {"open-questions.md": content}

    def _genuine(self, n: int, start: int = 1) -> str:
        return "".join(f"- [Q{start + i}] Genuine question {start + i}?\n" for i in range(n))

    def test_cap_enforced_with_trailer(self):
        # 12 genuine entries: exactly the first 10 render, plus one trailer
        # naming the 2 omitted.
        content = "# Open Questions\n\n" + self._genuine(12)
        result = build_l1(self._files(content), [])
        for i in range(1, 11):
            assert f"Genuine question {i}?" in result
        assert "Genuine question 11?" not in result
        assert "Genuine question 12?" not in result
        assert self._TRAILER_2 in result
        assert result.count("more open questions") == 1

    def test_trailer_absent_when_nothing_omitted(self):
        content = "# Open Questions\n\n" + self._genuine(10)
        result = build_l1(self._files(content), [])
        assert "Genuine question 10?" in result
        assert "more open questions" not in result

    def test_resolved_entries_excluded(self):
        # Entries under ## Resolved and resolved_by-annotated entries above
        # the divider drop out of L1 entirely.
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] Still open?\n"
            "- [Resolved by D7 on 2026-05-01] [Q2] Annotated above divider?\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D9 on 2026-05-02] [Q3] Under the divider?\n"
        )
        result = build_l1(self._files(content), [])
        assert "Still open?" in result
        assert "Annotated above divider?" not in result
        assert "Under the divider?" not in result
        assert "## Resolved" not in result

    def test_pointers_consume_no_slot_and_are_not_counted(self):
        # 12 genuine entries plus 3 interleaved pointers: 10 genuine render,
        # the trailer counts only the 2 omitted genuine entries, and no
        # pointer surfaces.
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q100] BRIEF: context/a.md — pointer one\n"
            + self._genuine(5)
            + "- [Q101] RESUME: context/b.md — pointer two\n"
            + self._genuine(7, start=6)
            + "- [Q102] SELECT: context/c.md — pointer three\n"
        )
        result = build_l1(self._files(content), [])
        for i in range(1, 11):
            assert f"Genuine question {i}?" in result
        assert "Genuine question 11?" not in result
        assert self._TRAILER_2 in result
        assert "BRIEF:" not in result
        assert "RESUME:" not in result
        assert "SELECT:" not in result

    def test_file_order_preserved(self):
        content = "# Open Questions\n\n" + self._genuine(12)
        result = build_l1(self._files(content), [])
        rendered_ids = [
            line.split("]")[0] + "]" for line in result.split("\n") if line.startswith("- [Q")
        ]
        assert rendered_ids[0] == "- [Q1]"
        assert rendered_ids[-1] == "- [Q10]"
        assert rendered_ids == [f"- [Q{i}]" for i in range(1, 11)]

    def test_whole_entries_never_split(self):
        # A multi-line entry in the last slot renders complete with its
        # continuation lines; the cap never cuts mid-entry.
        content = (
            "# Open Questions\n"
            "\n" + self._genuine(9) + "- [Q10] Tenth with detail?\n"
            "  continuation line one\n"
            "  continuation line two\n"
            "- [Q11] Beyond the cap?\n"
        )
        result = build_l1(self._files(content), [])
        assert "Tenth with detail?" in result
        assert "  continuation line one" in result
        assert "  continuation line two" in result
        assert "Beyond the cap?" not in result

    def test_empty_file_renders_no_section(self):
        result = build_l1(self._files(""), [])
        assert "Open Questions" not in result

    def test_missing_file_renders_no_section(self):
        result = build_l1({}, [])
        assert "Open Questions" not in result

    def test_all_resolved_file_renders_no_section(self):
        content = (
            "# Open Questions\n"
            "\n"
            "## Resolved\n"
            "\n"
            "- [Resolved by D5 on 2026-04-01] [Q1] Done and dusted?\n"
        )
        result = build_l1(self._files(content), [])
        assert "Open Questions" not in result
        assert "Done and dusted?" not in result

    def test_age_nudge_renders_at_l1(self):
        today = datetime.now(timezone.utc).date()
        target = today - timedelta(days=45)
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] Stale at L1?\n"
        result = build_l1(self._files(content), [])
        assert "(open 45 days; consider closing or deferring)" in result
        assert "Stale at L1?" in result

    def test_l0_cap_enforced_with_exact_trailer(self):
        content = "# Open Questions\n\n" + self._genuine(5)
        result = build_l0(self._files(content), [])

        assert L0_QUESTIONS_LIMIT == 3
        for i in range(1, 4):
            assert f"Genuine question {i}?" in result
        assert "Genuine question 4?" not in result
        assert "Genuine question 5?" not in result
        assert self._TRAILER_2 in result
        assert result.count("more open questions") == 1

    def test_l0_trailer_absent_at_or_below_cap(self):
        for count in (L0_QUESTIONS_LIMIT - 1, L0_QUESTIONS_LIMIT):
            content = "# Open Questions\n\n" + self._genuine(count)
            result = build_l0(self._files(content), [])

            assert f"Genuine question {count}?" in result
            assert "more open questions" not in result

    def test_l0_and_l1_use_identical_trailer_for_same_omitted_count(self):
        l0 = build_l0(
            self._files("# Open Questions\n\n" + self._genuine(L0_QUESTIONS_LIMIT + 2)),
            [],
        )
        l1 = build_l1(self._files("# Open Questions\n\n" + self._genuine(12)), [])

        l0_trailer = next(line for line in l0.splitlines() if line.startswith("(+"))
        l1_trailer = next(line for line in l1.splitlines() if line.startswith("(+"))
        assert l0_trailer == l1_trailer == self._TRAILER_2


class TestQuestionEntryCharBudget:
    """Each question entry's rendered text is capped at
    QUESTION_ENTRY_CHAR_BUDGET characters in L0 and L1.

    Entries at or under the budget render byte-unchanged; over-budget
    entries are cut and end with the pointer at the full file. L2 dumps
    open-questions.md verbatim and stays uncapped.
    """

    def _files(self, content: str) -> dict[str, str]:
        return {"open-questions.md": content}

    def _over_budget_entry(self) -> str:
        body = "Should we adopt the long option? " + "detail " * 100
        return f"- [Q1] {body.strip()}\n"

    def test_under_budget_entry_renders_byte_unchanged_at_l0(self):
        entry = "- [Q1] Short and sweet?"
        content = f"# Open Questions\n\n{entry}\n"
        result = build_l0(self._files(content), [])
        assert entry in result
        assert QUESTION_TRUNCATION_POINTER not in result

    def test_over_budget_entry_truncates_at_l0(self):
        content = "# Open Questions\n\n" + self._over_budget_entry()
        result = build_l0(self._files(content), [])
        assert QUESTION_TRUNCATION_POINTER in result
        assert "detail detail detail detail" in result  # leading text survives
        section = result[result.index("## Open Questions") :]
        entry_line = section.split("\n")[1]
        assert len(entry_line) <= QUESTION_ENTRY_CHAR_BUDGET + 1 + len(QUESTION_TRUNCATION_POINTER)

    def test_over_budget_entry_truncates_at_l1(self):
        content = "# Open Questions\n\n" + self._over_budget_entry()
        result = build_l1(self._files(content), [])
        assert QUESTION_TRUNCATION_POINTER in result

    def test_l0_and_l1_truncate_identically(self):
        content = "# Open Questions\n\n" + self._over_budget_entry()
        l0 = build_l0(self._files(content), [])
        l1 = build_l1(self._files(content), [])
        l0_line = next(line for line in l0.split("\n") if line.startswith("- [Q1]"))
        l1_line = next(line for line in l1.split("\n") if line.startswith("- [Q1]"))
        assert l0_line == l1_line

    def test_l2_keeps_full_text_verbatim(self):
        content = "# Open Questions\n\n" + self._over_budget_entry()
        result = build_l2(self._files(content), [])
        assert content.strip() in result
        assert QUESTION_TRUNCATION_POINTER not in result

    def test_multiline_entry_capped_on_joined_text(self):
        # The budget applies to the entry's joined rendered text (head plus
        # continuation lines), so a long continuation tail is cut too.
        long_tail = "  " + "tail " * 150
        content = f"# Open Questions\n\n- [Q1] Multi-line head?\n{long_tail}\n"
        result = build_l1(self._files(content), [])
        assert QUESTION_TRUNCATION_POINTER in result
        assert "Multi-line head?" in result

    def test_cap_leaves_age_projection_untouched(self):
        target = datetime.now(timezone.utc).date() - timedelta(days=45)
        body = "Old and long? " + "padding " * 100
        content = f"# Open Questions\n\n- [{_legacy_id(target)}] {body.strip()}\n"
        result = build_l0(self._files(content), [])
        assert "(open 45 days; consider closing or deferring)" in result
        assert QUESTION_TRUNCATION_POINTER in result

    def test_cap_leaves_omission_trailer_untouched(self):
        entries = "".join(f"- [Q{i}] Genuine question {i}?\n" for i in range(1, 13))
        content = "# Open Questions\n\n" + entries
        result = build_l1(self._files(content), [])
        assert "(+2 more open questions" in result
        assert QUESTION_TRUNCATION_POINTER not in result


class TestBuildL1StackProjection:
    """L1 renders a deterministic bounded projection of stack.md.

    The walk keeps top-level headings and first-level list items — ordered
    and unordered — outside fenced code blocks (nonblank paragraphs for a
    file with neither), truncates overlong items at
    STACK_AUTO_ITEM_CHAR_LIMIT retained characters with an explicit marker,
    and stops before rendering more than STACK_AUTO_ITEM_LIMIT items or
    letting the joined projection body (items plus inter-item separators)
    exceed STACK_AUTO_CHAR_BUDGET. The block opens with the
    non-authoritative framing line and, when items were omitted, closes
    with the exact omitted count and the get_raw_file route.
    """

    _TRUNCATION_MARKER = '... [item truncated - full text: get_raw_file("stack.md")]'

    def _files(self, stack: str) -> dict[str, str]:
        return {"stack.md": stack}

    def _stack_block(self, result: str) -> str:
        """The projection block: from the framing line to the next blank-line
        section break (or end of payload)."""
        start = result.index(STACK_NON_AUTHORITATIVE_FRAMING)
        end = result.find("\n\n", start)
        return result[start:] if end == -1 else result[start:end]

    def _projection_body(self, result: str) -> str:
        """The budget-bounded body: the block minus the framing line and
        any omitted-count trailer (both are annotations outside the
        budget)."""
        lines = self._stack_block(result).split("\n")
        assert lines[0] == STACK_NON_AUTHORITATIVE_FRAMING
        if lines[-1].startswith("(+"):
            lines = lines[:-1]
        return "\n".join(lines[1:])

    def test_framing_line_opens_the_block(self):
        result = build_l1(self._files("# Stack\n- **Python** — language\n"), [])
        block = self._stack_block(result)
        assert block.startswith(STACK_NON_AUTHORITATIVE_FRAMING)
        assert "- **Python** — language" in block

    def test_item_limit_stops_the_walk_with_exact_omitted_count(self):
        # 1 heading + 25 items = 26 items against a cap of 20: the first 19
        # items follow the heading, and the trailer names exactly 6 omitted.
        stack = "# Stack\n" + "".join(f"- item {i}\n" for i in range(1, 26))
        result = build_l1(self._files(stack), [])
        assert "- item 19" in result
        assert "- item 20" not in result
        assert "- item 25" not in result
        assert '(+6 more stack items — see get_raw_file("stack.md") for the full file)' in result

    def test_no_trailer_when_nothing_omitted(self):
        stack = "# Stack\n- one\n- two\n"
        result = build_l1(self._files(stack), [])
        assert "more stack items" not in result

    def test_overlong_item_truncated_with_marker(self):
        long_item = "- **Kafka** — " + "detail " * 100
        stack = f"# Stack\n{long_item.rstrip()}\n- short item\n"
        result = build_l1(self._files(stack), [])
        assert self._TRUNCATION_MARKER in result
        assert "- **Kafka**" in result
        assert "- short item" in result
        line = next(line for line in result.split("\n") if line.startswith("- **Kafka**"))
        assert len(line) <= STACK_AUTO_ITEM_CHAR_LIMIT + 1 + len(self._TRUNCATION_MARKER)

    def test_under_limit_item_renders_byte_unchanged(self):
        item = "- **Postgres** — primary store"
        result = build_l1(self._files(f"# Stack\n{item}\n"), [])
        assert item in result
        assert self._TRUNCATION_MARKER not in result

    def test_aggregate_budget_bounds_the_joined_body(self):
        # Eight 280-char items never trip the per-item limit, but the
        # joined body (heading + items + one separator newline per
        # boundary) would pass 2,000 at the eighth, so the walk stops
        # after seven and counts the last one omitted. The emitted body is
        # provably within the budget.
        items = ["- " + f"i{i} " + "x" * 275 for i in range(8)]
        assert all(len(item) == 280 for item in items)
        stack = "# Stack\n" + "\n".join(items) + "\n"
        result = build_l1(self._files(stack), [])
        assert "i6 " in result
        assert "i7 " not in result
        assert '(+1 more stack items — see get_raw_file("stack.md") for the full file)' in result
        assert len(self._projection_body(result)) <= STACK_AUTO_CHAR_BUDGET

    def test_separator_cost_counts_toward_the_budget(self):
        # Twenty exactly-100-char items sum to 2,000 alone, but the 19
        # separator newlines put the joined body over budget, so the
        # twentieth stops the walk. A counter ignoring separators would
        # render all twenty and emit 2,019 characters.
        items = [f"- i{i:02d} " + "x" * 94 for i in range(20)]
        assert all(len(item) == 100 for item in items)
        stack = "\n".join(items) + "\n"
        result = build_l1(self._files(stack), [])
        assert "- i18 " in result
        assert "- i19 " not in result
        assert '(+1 more stack items — see get_raw_file("stack.md") for the full file)' in result
        assert len(self._projection_body(result)) <= STACK_AUTO_CHAR_BUDGET

    def test_nested_list_items_excluded(self):
        stack = (
            "# Stack\n"
            "- **Python 3.11** — main language\n"
            "  - Chose over Go for ecosystem\n"
            "    - even deeper rationale\n"
            "- **AWS Lambda** — serverless\n"
        )
        result = build_l1(self._files(stack), [])
        assert "- **Python 3.11** — main language" in result
        assert "- **AWS Lambda** — serverless" in result
        assert "Chose over Go" not in result
        assert "even deeper rationale" not in result

    def test_hash_tag_line_does_not_trigger_structured_mode(self):
        # A prose file with a hash-tag word is not structured: the real
        # paragraphs must survive the walk instead of being discarded.
        stack = "We use #python on the backend.\n\n#TODO revisit the queue choice.\n"
        result = build_l1(self._files(stack), [])
        assert "We use #python on the backend." in result
        assert "#TODO revisit the queue choice." in result

    def test_hashes_without_following_space_are_not_headings(self):
        stack = "##foo prose line.\n\n#######x seven hashes prose.\n"
        result = build_l1(self._files(stack), [])
        assert "##foo prose line." in result
        assert "#######x seven hashes prose." in result

    def test_bare_hash_runs_are_headings(self):
        # "#" and "######" alone are valid ATX headings and stay items;
        # the same predicate drives structured-mode detection and item
        # classification, so both render.
        stack = "#\n- item one\n######\n- item two\n"
        result = build_l1(self._files(stack), [])
        body_lines = self._projection_body(result).split("\n")
        assert body_lines == ["#", "- item one", "######", "- item two"]

    def test_seven_hash_heading_is_prose_not_an_item(self):
        # Seven hashes exceed the ATX marker bound even with a space, so
        # the line is neither a heading item nor a structured trigger.
        stack = "# Stack\n####### not a heading\n- real item\n"
        result = build_l1(self._files(stack), [])
        assert "- real item" in result
        assert "not a heading" not in result

    def test_ordered_list_items_are_items(self):
        stack = "# Stack\n1. Python\n2. Postgres\n"
        result = build_l1(self._files(stack), [])
        assert "1. Python" in result
        assert "2. Postgres" in result
        assert "more stack items" not in result

    def test_paren_ordered_marker_recognized(self):
        result = build_l1(self._files("# Stack\n1) Python\n"), [])
        assert "1) Python" in result

    def test_ordered_items_consume_limit_and_report_omitted(self):
        # Heading + 25 ordered items against the 20-item cap: 19 items
        # follow the heading and the trailer names exactly 6 omitted.
        stack = "# Stack\n" + "".join(f"{i}. item {i}\n" for i in range(1, 26))
        result = build_l1(self._files(stack), [])
        assert "19. item 19" in result
        assert "20. item 20" not in result
        assert '(+6 more stack items — see get_raw_file("stack.md") for the full file)' in result

    def test_overlong_ordered_item_truncated_with_marker(self):
        stack = "# Stack\n1. " + "detail " * 100 + "\n"
        result = build_l1(self._files(stack), [])
        assert self._TRUNCATION_MARKER in result
        assert "1. detail" in result

    def test_indented_ordered_item_excluded(self):
        stack = "# Stack\n1. top level\n   1. nested rationale\n"
        result = build_l1(self._files(stack), [])
        assert "1. top level" in result
        assert "nested rationale" not in result

    def test_ten_digit_marker_is_not_a_list_item(self):
        stack = "# Stack\n1234567890. not a list item\n"
        result = build_l1(self._files(stack), [])
        assert "not a list item" not in result

    def test_fenced_code_lines_are_not_items(self):
        stack = "# Stack\n```bash\n# not a heading\n- not a bullet\n```\n- real item\n"
        result = build_l1(self._files(stack), [])
        assert "- real item" in result
        assert "# not a heading" not in result
        assert "- not a bullet" not in result
        assert "more stack items" not in result

    def test_fake_items_inside_fence_do_not_consume_budgets(self):
        # 25 heading-looking lines inside a fence: none are items, so the
        # real item after the fence renders and no omitted count appears.
        fake = "".join(f"# fake heading {i}\n" for i in range(25))
        stack = "# Stack\n```\n" + fake + "```\n- real item\n"
        result = build_l1(self._files(stack), [])
        assert "- real item" in result
        assert "fake heading" not in result
        assert "more stack items" not in result

    def test_shorter_fence_of_same_char_does_not_close(self):
        # A closer must be a matching-or-longer run of the same character:
        # the inner ``` cannot close the ```` fence.
        stack = "# Stack\n````\n```\n- still inside\n````\n- outside\n"
        result = build_l1(self._files(stack), [])
        assert "- outside" in result
        assert "- still inside" not in result

    def test_mismatched_fence_char_does_not_close(self):
        stack = "# Stack\n```\n~~~\n- still inside\n```\n- outside\n"
        result = build_l1(self._files(stack), [])
        assert "- outside" in result
        assert "- still inside" not in result

    def test_tilde_fence_lines_excluded(self):
        stack = "# Stack\n~~~\n- fake\n~~~\n- real\n"
        result = build_l1(self._files(stack), [])
        assert "- real" in result
        assert "- fake" not in result

    def test_unclosed_fence_runs_to_end_of_file(self):
        stack = "# Stack\n- real\n```\n- fake\n# also fake\n"
        result = build_l1(self._files(stack), [])
        assert "- real" in result
        assert "- fake" not in result
        assert "also fake" not in result

    def test_backtick_in_backtick_info_string_does_not_open_a_fence(self):
        # CommonMark forbids backticks in a backtick fence's info string:
        # the line is ordinary text, so the following items stay items.
        stack = "# Stack\n```py`bad\n- real item\n"
        result = build_l1(self._files(stack), [])
        assert "- real item" in result

    def test_backtick_in_tilde_info_string_still_opens_a_fence(self):
        # Tilde fences legitimately allow backticks in their info string.
        stack = "# Stack\n~~~py`ok\n- fake\n~~~\n- real\n"
        result = build_l1(self._files(stack), [])
        assert "- real" in result
        assert "- fake" not in result

    def test_entirely_fenced_file_renders_framing_only(self):
        result = build_l1(self._files("```\ncode only\n```\n"), [])
        assert STACK_NON_AUTHORITATIVE_FRAMING in result
        assert "code only" not in result

    def test_paragraphs_do_not_merge_across_an_elided_fence(self):
        # Unstructured mode: the fence drops out but leaves a paragraph
        # break, so the surrounding prose stays two items.
        stack = "First paragraph.\n```\ncode\n```\nSecond paragraph.\n"
        result = build_l1(self._files(stack), [])
        assert "First paragraph." in result
        assert "Second paragraph." in result
        assert "First paragraph.\nSecond paragraph." not in result
        assert "code" not in result

    def test_loose_prose_excluded_from_structured_file(self):
        stack = "# Stack\n\nSome loose prose about the stack.\n\n- **Python** — language\n"
        result = build_l1(self._files(stack), [])
        assert "- **Python** — language" in result
        assert "Some loose prose" not in result

    def test_unstructured_file_walks_nonblank_paragraphs(self):
        stack = (
            "We run Python on Lambda.\n"
            "The gateway fronts it.\n"
            "\n"
            "Postgres holds the primary data.\n"
        )
        result = build_l1(self._files(stack), [])
        assert "We run Python on Lambda.\nThe gateway fronts it." in result
        assert "Postgres holds the primary data." in result

    def test_unstructured_paragraphs_respect_item_limit(self):
        paragraphs = "\n\n".join(f"Paragraph number {i} prose." for i in range(1, 24))
        result = build_l1(self._files(paragraphs), [])
        assert "Paragraph number 20 prose." in result
        assert "Paragraph number 21 prose." not in result
        assert '(+3 more stack items — see get_raw_file("stack.md") for the full file)' in result

    def test_l0_oneliner_untouched_by_projection(self):
        # The L0 stack surface stays the one-line extraction — no framing,
        # no projection annotations. Pinned so the generated AGENTS.md
        # bytes cannot drift.
        files = {
            "stack.md": (
                "# Stack\n"
                "## Language\n"
                "- **Python 3.11+** — main language\n"
                "## Infrastructure\n"
                "- **AWS Lambda** — serverless\n"
            )
        }
        result = build_l0(files, [])
        assert "**Stack:** Python 3.11+, AWS Lambda" in result
        assert STACK_NON_AUTHORITATIVE_FRAMING not in result
        assert "more stack items" not in result


class TestL2StackFraming:
    """L2 keeps the complete stack.md body, framed as non-authoritative."""

    def test_framing_line_precedes_verbatim_body(self):
        result = build_l2(FULL_FILES, DECISIONS)
        assert STACK_NON_AUTHORITATIVE_FRAMING in result
        assert result.index(STACK_NON_AUTHORITATIVE_FRAMING) < result.index("# Stack")
        # The body itself stays verbatim, nested rationale included.
        assert "Chose over Go for ecosystem" in result

    def test_no_projection_annotations_at_l2(self):
        stack = "# Stack\n" + "".join(f"- item {i}\n" for i in range(1, 30))
        result = build_l2({"stack.md": stack}, [])
        assert "- item 29" in result
        assert "more stack items" not in result

    def test_no_framing_without_stack_file(self):
        result = build_l2({"project.md": "# P\n\nGoal.\n"}, [])
        assert STACK_NON_AUTHORITATIVE_FRAMING not in result
