"""Tests for nauro_core.context — L0/L1/L2 context assembly."""

from datetime import date as _date

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
    "questions.md": (
        "# Open Questions\n"
        "- [2026-01-01 UTC] How does auth work?\n"
        "- [2026-01-02 UTC] What about caching?\n"
        "- [2026-01-03 UTC] Redis or Memcached?\n"
        "- [2026-01-04 UTC] Deploy strategy?\n"
        "- [2026-01-05 UTC] Monitoring setup?\n"
        "- [2026-01-06 UTC] Sixth question?\n"
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
