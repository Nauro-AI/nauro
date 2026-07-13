"""Kernel-level tests for ``operations.check_decision`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` with parseable v2 decisions and asserts
on the typed :class:`CheckDecisionResult` directly. Surface-level wiring
tests live in each transport's own suite.
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import _seed_decision, _store_with

from nauro_core.constants import (
    LEXICAL_RANK_CAVEAT,
    MAX_APPROACH_LENGTH,
    MAX_CONTEXT_LENGTH,
    NO_DECISIONS_TO_CHECK,
    NO_RELATED_DECISIONS,
)
from nauro_core.decision_model import DecisionStatus
from nauro_core.operations import (
    CheckDecisionResult,
    InMemoryStore,
    check_decision,
)
from nauro_core.operations.check_decision import _assessment
from nauro_core.operations.results import RelatedDecision


def test_returns_result_type() -> None:
    result = check_decision(InMemoryStore(), "Use Redis for caching")
    assert isinstance(result, CheckDecisionResult)


def test_empty_store_returns_no_decisions_assessment() -> None:
    result = check_decision(InMemoryStore(), "Use Redis for caching")
    assert result.related_decisions == []
    assert result.assessment == NO_DECISIONS_TO_CHECK
    assert result.error is None


def test_unrelated_proposal_returns_no_related_decisions() -> None:
    """A proposal sharing no salient tokens with any decision falls into the
    'no related decisions' branch, distinct from the empty-store branch."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID semantics for our transactional workload."),
    )
    result = check_decision(store, "Pineapple production logistics across mid-atlantic ports")
    assert result.related_decisions == []
    assert result.assessment == NO_RELATED_DECISIONS
    assert result.error is None


def test_no_match_assessment_does_not_read_as_all_clear() -> None:
    """The no-keyword-match assessment must name the lexical limitation so a
    paraphrase miss is visibly distinct from 'nothing exists' — and distinct
    from the empty-store message. Guards the silent-miss fix."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID semantics for our transactional workload."),
    )
    result = check_decision(store, "Pineapple production logistics across mid-atlantic ports")
    # A populated store that returns no hits is NOT the empty-store case.
    assert result.assessment != NO_DECISIONS_TO_CHECK
    lowered = result.assessment.lower()
    assert "lexical" in lowered or "keyword" in lowered
    assert "nothing exists" in lowered


def test_related_decision_canonical_id_and_status_enrichment() -> None:
    """Hits expose canonical id, status, and date fields from the parsed decision."""
    store = _store_with(
        _seed_decision(
            42,
            "Adopt PostgreSQL",
            "Use PostgreSQL for ACID transactional semantics across the platform.",
            decision_date=date(2026, 4, 16),
        ),
    )
    result = check_decision(store, "Migrate primary storage to PostgreSQL")
    assert result.error is None
    assert len(result.related_decisions) == 1
    hit = result.related_decisions[0]
    assert hit.id == "decision-042"
    assert hit.title == "Adopt PostgreSQL"
    assert hit.status == "active"
    assert hit.date == "2026-04-16"
    assert hit.score > 0.0
    assert "PostgreSQL" in hit.rationale_preview


def test_assessment_single_match_directs_to_get_decision() -> None:
    store = _store_with(
        _seed_decision(7, "Adopt Redis", "Use Redis for session cache with TTL eviction."),
    )
    result = check_decision(store, "Add Redis as a session cache")
    assert result.error is None
    assert "Top match: D007" in result.assessment
    assert LEXICAL_RANK_CAVEAT in result.assessment
    assert "Call get_decision(7) before proposing." in result.assessment


def test_assessment_multi_match_directs_to_each() -> None:
    store = _store_with(
        _seed_decision(7, "Adopt Redis", "Use Redis for session cache with TTL eviction."),
        _seed_decision(8, "Adopt Memcached", "Use Memcached for the legacy session cache."),
    )
    result = check_decision(store, "Pick a session cache implementation")
    assert result.error is None
    assert len(result.related_decisions) >= 2
    assert "Found" in result.assessment
    assert LEXICAL_RANK_CAVEAT in result.assessment
    assert "Call get_decision on each related decision before proposing." in result.assessment


def test_superseded_decisions_are_excluded_from_hits() -> None:
    """Retrieval narrows to active decisions; superseded entries never surface.

    Matches existing local behavior — ``bm25_retrieve`` filters by status
    before scoring. Locks in the contract so a regression in retrieval would
    show up as a hit count change here, not a silent escalation surface.
    """
    store = _store_with(
        _seed_decision(
            5,
            "Adopt REST endpoints",
            "Initial transport choice for the public API, later replaced by gRPC.",
            status=DecisionStatus.superseded,
        ),
        _seed_decision(
            6,
            "Adopt gRPC for internal API",
            "Replace REST with gRPC for cross-service calls on the internal mesh.",
        ),
    )
    result = check_decision(store, "Use gRPC instead of REST for internal services")
    assert result.error is None
    ids = [hit.id for hit in result.related_decisions]
    assert "decision-006" in ids
    assert "decision-005" not in ids


def test_context_arg_joins_into_retrieval_query() -> None:
    """Tokens that appear only in ``context`` still drive retrieval."""
    store = _store_with(
        _seed_decision(
            3,
            "Adopt Kafka",
            "Replace RabbitMQ on the event spine for higher throughput tolerance.",
        ),
    )
    # The approach text alone shares no salient tokens with the seeded
    # decision; the only overlap rides in via ``context``.
    result = check_decision(
        store,
        "Pick a streaming substrate for downstream consumers",
        context="Migrating off RabbitMQ.",
    )
    assert result.error is None
    assert len(result.related_decisions) >= 1
    assert result.related_decisions[0].id == "decision-003"


def test_rejection_when_approach_over_length() -> None:
    store = _store_with(_seed_decision(1, "Adopt PostgreSQL", "ACID semantics for our workload."))
    result = check_decision(store, "x" * (MAX_APPROACH_LENGTH + 1))
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert str(MAX_APPROACH_LENGTH) in result.error.reason
    assert result.related_decisions == []
    assert result.assessment == ""


def test_rejection_when_context_over_length() -> None:
    store = _store_with(_seed_decision(1, "Adopt PostgreSQL", "ACID semantics for our workload."))
    result = check_decision(
        store,
        "Use Redis",
        context="y" * (MAX_CONTEXT_LENGTH + 1),
    )
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert str(MAX_CONTEXT_LENGTH) in result.error.reason


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = check_decision(InMemoryStore(), "Use Redis for caching")
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


@pytest.mark.parametrize("missing_body_stem", ["999-vanished"])
def test_missing_body_is_skipped_not_fatal(missing_body_stem: str) -> None:
    """If list_decisions surfaces a stem with no body, the operation skips it.

    Defensive: a backend may race a delete against an enumeration. The kernel
    must not crash on the inconsistent view.
    """

    class _RaceyStore(InMemoryStore):
        def __init__(self) -> None:
            stem, body = _seed_decision(1, "Adopt PostgreSQL", "ACID semantics for our workload.")
            super().__init__(decisions={stem: body})
            self._missing_stem = missing_body_stem

        def list_decisions(self) -> list[str]:
            return sorted([*super().list_decisions(), self._missing_stem])

    result = check_decision(_RaceyStore(), "Migrate to PostgreSQL")
    assert result.error is None
    assert any(hit.id == "decision-001" for hit in result.related_decisions)


def test_scaffold_seed_excluded_from_retrieval() -> None:
    """The scaffold-seeded (num=1, "Initial project setup") decision never
    surfaces in retrieval — it records that the store was initialized, not
    a user choice. Locks in the curation moved from the pre-cutover wrapper.
    """
    store = _store_with(
        _seed_decision(
            1,
            "Initial project setup",
            "Scaffolded by nauro init to bootstrap the decision store.",
        ),
        _seed_decision(
            7,
            "Adopt PostgreSQL",
            "ACID semantics for the transactional workload backing the API.",
        ),
    )
    result = check_decision(store, "Migrate primary storage to PostgreSQL")
    assert result.error is None
    ids = [hit.id for hit in result.related_decisions]
    assert "decision-001" not in ids
    assert "decision-007" in ids


def test_use_stopword_does_not_force_false_positive() -> None:
    """``use`` is a near-universal token in decision titles; treating it as
    a stopword collapses the spurious matches that surface a proposal as
    near-neighbour to almost every prior decision. Locks the curation
    from ``TIER2_STOPWORDS``.
    """
    store = _store_with(
        _seed_decision(
            2,
            "Adopt PostgreSQL",
            "Use PostgreSQL for ACID transactional semantics.",
        ),
        _seed_decision(
            3,
            "Adopt Redis",
            "Use Redis for session cache eviction.",
        ),
        _seed_decision(
            4,
            "Adopt Kafka",
            "Use Kafka for the event spine across services.",
        ),
    )
    # Proposal shares only the ``use`` stem with the seeded decisions —
    # everything else is unrelated subject matter.
    result = check_decision(store, "Use pineapples for mid-atlantic logistics")
    assert result.error is None
    assert result.related_decisions == []


def test_long_approach_is_capped_for_retrieval() -> None:
    """``proposed_approach`` over 200 chars has tokens past the cap dropped
    from the BM25 input. Tokens before the cap still retrieve normally.
    Locks the 100/200 truncation contract carried over from the
    pre-cutover ``pseudo_proposal`` construction.
    """
    store = _store_with(
        _seed_decision(
            5,
            "Adopt Cassandra",
            "Cassandra for wide-column write-heavy workloads.",
        ),
        _seed_decision(
            6,
            "Adopt PostgreSQL",
            "PostgreSQL for ACID transactional semantics.",
        ),
    )
    # First 200 chars are unrelated filler; the Cassandra-salient token
    # sits at the tail, beyond the truncation cap, so it never reaches
    # the BM25 index. Stays under MAX_APPROACH_LENGTH (5_000) so we
    # exercise the curation, not the rejection branch.
    filler = "x" * 2_000
    approach = filler + " choose Cassandra for the new write path"
    result = check_decision(store, approach)
    assert result.error is None
    ids = [hit.id for hit in result.related_decisions]
    assert "decision-005" not in ids


def test_assessment_embedding_sourced_top_hit_not_labeled_bm25() -> None:
    """An embedding-sourced top hit carries score 0.0 (no BM25 score); the
    assessment must call it a semantic match, never 'BM25 0.0'. Exercises the
    union path's embedding-only branch at the assessment layer without the
    optional embeddings dependency."""
    hit = RelatedDecision(
        id="decision-042",
        title="Adopt PostgreSQL",
        score=0.0,
        status="active",
        date="2026-04-16",
        rationale_preview="ACID semantics for the platform.",
    )
    assessment = _assessment([hit])
    assert "Top match: D042" in assessment
    assert "semantic match" in assessment
    assert "BM25 0.0" not in assessment
    assert LEXICAL_RANK_CAVEAT in assessment
