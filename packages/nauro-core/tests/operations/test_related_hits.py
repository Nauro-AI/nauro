"""Kernel tests for the shared triage projection in ``operations.related_hits``.

The module is the single hit-lifting path behind ``check_decision`` and
``propose_decision`` and owns the lede/frontmatter primitives that
``get_decision``'s header mode shares. These tests pin the enrichment
contract at the helper level; each operation's own suite covers the
call-site wiring.
"""

from __future__ import annotations

from datetime import date

from conftest import _seed_decision, _store_with, make_decision

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    DecisionType,
)
from nauro_core.operations import check_decision, propose_decision
from nauro_core.operations.related_hits import (
    LEDE_MAX_CHARS,
    decision_lede,
    to_related_decisions,
)


def _decision(**overrides) -> Decision:
    defaults = dict(
        date=date(2026, 4, 16),
        confidence=DecisionConfidence.high,
        status=DecisionStatus.active,
        decision_type=DecisionType.architecture,
        supersedes="7",
        num=42,
        title="Adopt PostgreSQL",
        rationale="ACID semantics for the platform.",
    )
    defaults.update(overrides)
    return Decision(**defaults)


def _hit(num: int = 42, **overrides) -> dict:
    defaults = dict(
        number=num,
        title="Adopt PostgreSQL",
        similarity=3.5,
        rationale_preview="raw hit preview",
    )
    defaults.update(overrides)
    return defaults


def test_lift_enriches_triage_fields_from_parsed_decision() -> None:
    [related] = to_related_decisions([_hit()], [_decision()])
    assert related.id == "decision-042"
    assert related.score == 3.5
    assert related.status == "active"
    assert related.date == "2026-04-16"
    assert related.decision_type == "architecture"
    assert related.confidence == "high"
    assert related.supersedes == "7"
    assert related.superseded_by is None


def test_lift_preview_is_the_header_lede_not_the_raw_hit_preview() -> None:
    """The preview carries the same first-paragraph lede the header mode
    projects, so the inline hit substitutes for a ``mode=header`` call."""
    rationale = "Lead paragraph carries the decision.\n\nSecond paragraph is detail."
    [related] = to_related_decisions([_hit()], [_decision(rationale=rationale)])
    assert related.rationale_preview == "Lead paragraph carries the decision."
    assert related.rationale_preview == decision_lede(rationale)


def test_lift_lede_truncates_at_shared_budget() -> None:
    rationale = "y" * (LEDE_MAX_CHARS + 100)
    [related] = to_related_decisions([_hit()], [_decision(rationale=rationale)])
    assert len(related.rationale_preview) <= LEDE_MAX_CHARS
    assert related.rationale_preview.endswith("…")
    assert related.rationale_preview == decision_lede(rationale)


def test_lift_unset_optionals_stay_none_and_drop_on_exclude_none() -> None:
    plain = _decision(decision_type=None, supersedes=None)
    [related] = to_related_decisions([_hit()], [plain])
    dumped = related.model_dump(mode="json", exclude_none=True)
    assert "decision_type" not in dumped
    assert "supersedes" not in dumped
    assert "superseded_by" not in dumped
    assert dumped["confidence"] == "high"


def test_lift_missing_decision_falls_back_to_raw_preview_and_none_fields() -> None:
    """Defensive miss (hit without a parsed decision, e.g. a backend racing
    a delete against enumeration): keep the raw hit preview, leave every
    enrichment field unset."""
    [related] = to_related_decisions([_hit(num=999)], [_decision()])
    assert related.id == "decision-999"
    assert related.status == "active"
    assert related.date == ""
    assert related.rationale_preview == "raw hit preview"
    assert related.decision_type is None
    assert related.confidence is None
    assert related.supersedes is None
    assert related.superseded_by is None


def test_lift_embedding_hit_without_similarity_scores_zero() -> None:
    [related] = to_related_decisions([_hit(similarity=None)], [_decision()])
    assert related.score == 0.0


def test_check_and_propose_call_sites_produce_identical_hit_shape() -> None:
    """Both operations lift through the shared helper: the same underlying
    decision surfaces with identical fields (score excepted — the two
    retrieval queries differ by construction)."""
    seed = _seed_decision(
        1,
        "Adopt PostgreSQL primary database",
        "Mature ecosystem with strong JSON support and excellent tooling.",
        confidence=DecisionConfidence.high,
        decision_type=DecisionType.architecture,
        decision_date=date(2026, 4, 16),
    )

    check_result = check_decision(
        _store_with(seed), "Use PostgreSQL for the primary database layer"
    )
    propose_result = propose_decision(
        _store_with(seed),
        title="Use PostgreSQL for the data layer",
        rationale="Better JSON handling than alternatives for our application data.",
        confidence="medium",
    )

    check_hits = {h.id: h for h in check_result.related_decisions}
    propose_hits = {h.id: h for h in propose_result.similar_decisions}
    assert "decision-001" in check_hits
    assert "decision-001" in propose_hits

    check_dump = check_hits["decision-001"].model_dump(mode="json", exclude_none=True)
    propose_dump = propose_hits["decision-001"].model_dump(mode="json", exclude_none=True)
    assert check_dump.keys() == propose_dump.keys()
    check_dump.pop("score")
    propose_dump.pop("score")
    assert check_dump == propose_dump
    assert check_dump["decision_type"] == "architecture"
    assert check_dump["confidence"] == "high"


def test_make_decision_helper_supersedes_field_flows_through() -> None:
    """An active decision that supersedes an older one carries the ref;
    ``superseded_by`` stays absent on active hits."""
    superseding = make_decision(3, "Adopt gRPC", "Replace REST on the internal mesh.")
    superseding = superseding.model_copy(update={"supersedes": "2"})
    [related] = to_related_decisions([_hit(num=3, title="Adopt gRPC")], [superseding])
    assert related.supersedes == "2"
    assert related.superseded_by is None
