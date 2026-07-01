"""Tests for nauro_core.context — L0/L1/L2 context assembly."""

from datetime import date as _date
from datetime import datetime, timedelta, timezone

from nauro_core.context import build_l0, build_l1, build_l2
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
)


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
        assert "# MyProject" in result
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
        assert "# MyProject" in result

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


class TestBuildL1:
    def test_canonical_ordering(self):
        result = build_l1(FULL_FILES, DECISIONS)
        project_pos = result.find("# MyProject")
        state_pos = result.find("# State")
        stack_pos = result.find("# Stack")
        questions_pos = result.find("# Open Questions")
        decisions_pos = result.find("## Decisions")
        assert project_pos < state_pos < stack_pos < questions_pos < decisions_pos

    def test_full_stack_included(self):
        result = build_l1(FULL_FILES, DECISIONS)
        assert "Chose over Go for ecosystem" in result

    def test_full_questions_included(self):
        result = build_l1(FULL_FILES, DECISIONS)
        assert "Sixth question?" in result

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
        # the full dump, and L2 must additionally carry superseded decisions.
        l1 = build_l1(FULL_FILES, DECISIONS)
        l2 = build_l2(FULL_FILES, DECISIONS)
        for marker in ("# MyProject", "Chose over Go for ecosystem", "Sixth question?"):
            assert marker in l1 and marker in l2
        assert "Old choice" in l2 and "Old choice" not in l1

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
        # Honours L0_QUESTIONS_LIMIT (3). The fourth open entry is dropped
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
        # SELECT: checkpoints (nauro-loop candidate sets, D322) are discovery
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
        # With limit = 3, three genuine questions plus two pointers should all
        # three genuine questions appear (pointers do not count toward the cap).
        content = (
            "# Open Questions\n"
            "\n"
            "- [Q1] BRIEF: context/a.md — pointer one\n"
            "- [Q2] First genuine question?\n"
            "- [Q3] Second genuine question?\n"
            "- [Q4] RESUME: context/b.md — pointer two\n"
            "- [Q5] Third genuine question?\n"
            "- [Q6] Fourth genuine question — should be cut by limit?\n"
        )
        result = build_l0(self._files(content), [])
        assert "First genuine question?" in result
        assert "Second genuine question?" in result
        assert "Third genuine question?" in result
        assert "Fourth genuine question" not in result
        assert "BRIEF:" not in result
        assert "RESUME:" not in result

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
