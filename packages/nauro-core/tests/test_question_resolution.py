"""Tests for the deterministic question-resolution seam."""

from __future__ import annotations

from datetime import date

import pytest

from nauro_core.question_resolution import (
    QuestionResolutionOutcome,
    ResolutionDecisionDocument,
    resolve_question_document,
)

RESOLUTION_DATE = date(2026, 8, 18)


def _decision(
    num: int,
    *,
    status: str = "active",
    superseded_by: str = "null",
    stem: str | None = None,
) -> ResolutionDecisionDocument:
    resolved_stem = stem or f"{num:03d}-decision-{num}"
    content = (
        "---\n"
        "date: 2026-08-18\n"
        "version: 1\n"
        f"status: {status}\n"
        "confidence: high\n"
        "decision_type: pattern\n"
        "reversibility: easy\n"
        "source: mcp\n"
        "files_affected: []\n"
        "supersedes: null\n"
        f"superseded_by: {superseded_by}\n"
        "---\n\n"
        f"# {num:03d} \u2014 Decision {num}\n\n"
        "## Decision\n\n"
        "Resolve the named question.\n"
    )
    return ResolutionDecisionDocument(stem=resolved_stem, content=content.encode())


def _resolve(
    content: str,
    *,
    decisions: tuple[ResolutionDecisionDocument, ...] | None = None,
    targets: tuple[str, ...] = ("Q5",),
    resolved_by: str = "D42",
) -> QuestionResolutionOutcome:
    return resolve_question_document(
        open_questions_bytes=content.encode(),
        decision_documents=(_decision(42),) if decisions is None else decisions,
        targets=targets,
        resolved_by=resolved_by,
        resolution_date=RESOLUTION_DATE,
    )


def test_resolves_and_reports_only_actual_transition() -> None:
    outcome = _resolve("# Open Questions\n\n- [Q1] open\n- [Q5] target\n")

    assert outcome.result.status == "ok"
    assert outcome.result.relocated_ids == ("Q5",)
    assert outcome.resolved_question_ids == ("Q5",)
    assert outcome.updated_open_questions_bytes is not None
    updated = outcome.updated_open_questions_bytes.decode()
    assert "[Resolved by D42 on 2026-08-18] [Q5] target" in updated


def test_different_decision_no_op_does_not_report_requested_anchor() -> None:
    content = (
        "# Open Questions\n\n"
        "## Resolved\n\n"
        "- [Resolved by D41 on 2026-08-17] [Q5] already closed\n"
    )
    outcome = _resolve(content)

    assert outcome.result.status == "ok"
    assert outcome.resolved_question_ids == ()
    assert outcome.updated_open_questions_bytes == content.encode()


def test_normalization_only_reports_empty_resolution_effect() -> None:
    content = (
        "# Open Questions\n\n"
        "- [Resolved by D41 on 2026-08-17] [Q5] stray\n"
        "- [Q9] open\n\n"
        "## Resolved\n"
    )
    outcome = _resolve(content)

    assert outcome.result.status == "ok"
    assert outcome.result.relocated_ids == ("Q5",)
    assert outcome.resolved_question_ids == ()
    assert outcome.updated_open_questions_bytes is not None
    updated = outcome.updated_open_questions_bytes.decode()
    assert "Resolved by D41" in updated
    assert "Resolved by D42" not in updated


def test_duplicate_targets_report_one_actual_transition() -> None:
    outcome = _resolve(
        "# Open Questions\n\n- [Q17] target\n",
        targets=("Q017", "Q17", "Q017"),
    )

    assert outcome.resolved_question_ids == ("Q17",)
    assert outcome.result.relocated_ids == ("Q17",)
    assert outcome.updated_open_questions_bytes is not None
    assert b"[Q17] target" in outcome.updated_open_questions_bytes


def test_legacy_timestamp_identity_is_preserved() -> None:
    legacy_id = "2026-04-30 10:00 UTC"
    outcome = _resolve(
        f"# Open Questions\n\n- [{legacy_id}] target\n",
        targets=(legacy_id,),
    )

    assert outcome.resolved_question_ids == (legacy_id,)
    assert outcome.updated_open_questions_bytes is not None
    assert f"[{legacy_id}] target" in outcome.updated_open_questions_bytes.decode()


@pytest.mark.parametrize(
    ("decisions", "reason"),
    [
        ((), "does not exist"),
        ((_decision(42), _decision(42, stem="042-duplicate")), "duplicate-number"),
        ((_decision(42, status="superseded", superseded_by='"43"'),), "not active"),
        ((ResolutionDecisionDocument(stem="042-bad", content=b"not markdown"),), "malformed"),
        ((ResolutionDecisionDocument(stem="042-bad", content=b"\xff"),), "UTF-8"),
    ],
)
def test_invalid_decision_evidence_rejects_without_effects(
    decisions: tuple[ResolutionDecisionDocument, ...],
    reason: str,
) -> None:
    outcome = _resolve(
        "# Open Questions\n\n- [Q5] target\n",
        decisions=decisions,
    )

    assert outcome.result.status == "rejected"
    assert outcome.result.error is not None
    assert reason in outcome.result.error.reason
    assert outcome.updated_open_questions_bytes is None
    assert outcome.resolved_question_ids == ()


def test_malformed_resolved_by_rejects_without_effects() -> None:
    outcome = _resolve(
        "# Open Questions\n\n- [Q5] target\n",
        resolved_by="not-a-decision",
    )

    assert outcome.result.status == "rejected"
    assert outcome.updated_open_questions_bytes is None
    assert outcome.resolved_question_ids == ()


@pytest.mark.parametrize(
    "content",
    [
        "# Open Questions\n\n- [Q1] other\n",
        "# Open Questions\n\n- [Q5] first\n- [Q005] second\n",
    ],
)
def test_invalid_target_set_rejects_without_effects(content: str) -> None:
    outcome = _resolve(content)

    assert outcome.result.status == "rejected"
    assert outcome.updated_open_questions_bytes is None
    assert outcome.resolved_question_ids == ()


@pytest.mark.parametrize(
    ("decisions", "targets", "resolved_by"),
    [
        ((_decision(42),), ("Q5",), "D42"),
        ((), (), "not-a-decision"),
    ],
)
def test_invalid_open_question_utf8_raises_without_an_outcome(
    decisions: tuple[ResolutionDecisionDocument, ...],
    targets: tuple[str, ...],
    resolved_by: str,
) -> None:
    with pytest.raises(UnicodeDecodeError):
        resolve_question_document(
            open_questions_bytes=b"\xff",
            decision_documents=decisions,
            targets=targets,
            resolved_by=resolved_by,
            resolution_date=RESOLUTION_DATE,
        )


def test_same_inputs_produce_same_outcome() -> None:
    content = "# Open Questions\n\n- [Q5] target\n"

    first = _resolve(content)
    second = _resolve(content)

    assert first == second


def test_decision_document_copies_mutable_input() -> None:
    content = bytearray(_decision(42).content)
    document = ResolutionDecisionDocument(stem="042-decision", content=content)

    content[0] = ord("x")

    assert document.content.startswith(b"---")
