"""Decision order on a filesystem store whose stems span 3 and 4 digits."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import get_context

from nauro.constants import DECISIONS_DIR
from nauro.mcp.payloads import build_l0_payload
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.reader import _list_decisions
from nauro.store.validator import validate_store

FOUR_DIGIT_NUMBERS = (998, 999, 1000, 1001)


def _decision_body(num: int) -> str:
    return format_decision(
        Decision(
            num=num,
            title=f"Decision {num}",
            rationale=f"Rationale for the {num}th decision.",
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
            date=date(2026, 1, 1),
        )
    )


@pytest.fixture
def store_past_999(tmp_path: Path) -> Path:
    """A store whose newest decision is 1001."""
    store_path = tmp_path / "project"
    decisions_dir = store_path / DECISIONS_DIR
    decisions_dir.mkdir(parents=True)
    (store_path / "project.md").write_text("# Project\n\nGoal: ship it.\n", encoding="utf-8")
    for num in FOUR_DIGIT_NUMBERS:
        path = decisions_dir / f"{num:03d}-decision-{num}.md"
        path.write_text(_decision_body(num), encoding="utf-8")
    return store_path


def test_filenames_really_disagree_with_number_order(store_past_999: Path) -> None:
    """Sanity check: name order and number order disagree in this store."""
    names = sorted(p.name for p in (store_past_999 / DECISIONS_DIR).glob("*.md"))
    assert names[-1] == "999-decision-999.md"


def test_reader_lists_decisions_in_number_order(store_past_999: Path) -> None:
    assert [d.num for d in _list_decisions(store_past_999)] == list(FOUR_DIGIT_NUMBERS)


def test_l0_recent_decisions_leads_with_the_newest(store_past_999: Path) -> None:
    summary = build_l0_payload(store_past_999).split("## Recent Decisions\n", 1)[1]
    assert [line.split(" ")[1] for line in summary.strip().splitlines()] == [
        "D1001",
        "D1000",
        "D999",
        "D998",
    ]


def test_l1_decisions_lead_with_the_newest(store_past_999: Path) -> None:
    result = get_context(FilesystemStore(store_past_999), 1)
    assert result.content is not None
    body = result.content.split("## Decisions\n\n", 1)[1]
    positions = [body.index(f"Decision {num}") for num in reversed(FOUR_DIGIT_NUMBERS)]
    assert positions == sorted(positions)


def test_numbering_gap_check_spans_min_to_max_not_first_to_last_name(
    store_past_999: Path,
) -> None:
    """998..1001 is a contiguous run, so no gap is reported for it."""
    gaps = [w for w in validate_store(store_past_999) if "numbering gap" in w]
    assert gaps == []
