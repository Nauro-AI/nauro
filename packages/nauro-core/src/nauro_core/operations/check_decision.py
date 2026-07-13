"""``check_decision`` retrieves related decisions for assessment.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`CheckDecisionResult`. Each transport's adapter wraps the call to
add transport-specific framing (``store`` field, telemetry emission,
exit-code handling), but the retrieval, ranking, and assessment text are
shared by construction.
"""

from __future__ import annotations

from bm25s.stopwords import STOPWORDS_EN

from nauro_core.constants import (
    LEXICAL_RANK_CAVEAT,
    MAX_APPROACH_LENGTH,
    MAX_CONTEXT_LENGTH,
    NO_DECISIONS_TO_CHECK,
    NO_RELATED_DECISIONS,
)
from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.decision_lookup import parse_all_decisions
from nauro_core.operations.results import (
    CheckDecisionResult,
    ErrorPayload,
    RelatedDecision,
)
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _canonical_decision_id,
    _decision_label,
    extract_decision_number,
)
from nauro_core.search import Bm25Hit, union_retrieve
from nauro_core.validation import check_content_length, is_scaffold_seed

# Extended stopword list for ``check_decision`` retrieval. Mirrors the
# tier-2 ``TIER2_STOPWORDS`` curation: bm25s's default English list omits
# common action verbs that appear in almost every decision title, so adding
# ``"use"`` collapses the false-positive matches that otherwise surface as
# near-neighbours on every call.
_CHECK_DECISION_STOPWORDS = [*list(STOPWORDS_EN), "use"]


def check_decision(
    store: Store,
    proposed_approach: str,
    context: str | None = None,
    use_embeddings: bool = False,
) -> CheckDecisionResult:
    """Return related-decision retrieval and assessment for ``proposed_approach``.

    Args:
        store: Storage adapter providing ``list_decisions`` / ``read_decision``.
        proposed_approach: Free-form description of the approach to check.
        context: Optional additional context concatenated into the retrieval
            query. Subject to ``MAX_CONTEXT_LENGTH``.
        use_embeddings: When True, augment the BM25 candidate pool with the
            optional embedding retriever (union). Resolved by the adapter from
            env/config; the kernel stays I/O-free. Fail-open: if the optional
            dependency is absent the result is BM25-only.

    Returns:
        :class:`CheckDecisionResult`. On the rejection path ``error`` is
        populated and ``related_decisions`` / ``assessment`` stay empty.
    """
    rejection = check_content_length(proposed_approach, "Proposed approach", MAX_APPROACH_LENGTH)
    if rejection:
        return CheckDecisionResult(error=ErrorPayload(kind="rejected", reason=rejection))
    if context:
        rejection = check_content_length(context, "Context", MAX_CONTEXT_LENGTH)
        if rejection:
            return CheckDecisionResult(error=ErrorPayload(kind="rejected", reason=rejection))

    decisions = parse_all_decisions(store)
    decisions = [d for d in decisions if not is_scaffold_seed(d)]
    if not decisions:
        return CheckDecisionResult(assessment=NO_DECISIONS_TO_CHECK)

    # The BM25 input envelope is fixed for byte-parity across surfaces:
    # title-style head (capped at 100) joined to the full approach + context
    # (capped at 200). The bm25s tokenizer is order-insensitive, but the
    # 100/200 cap shapes which tokens reach the index — the
    # ``pseudo_proposal`` truncation locks the same retrieval surface.
    approach_head = proposed_approach[:100]
    body_text = proposed_approach + (f" {context}" if context else "")
    query_text = f"{approach_head}. {body_text[:200]}"
    hits = union_retrieve(
        decisions,
        query_text,
        top_k=5,
        stopwords=_CHECK_DECISION_STOPWORDS,
        use_embeddings=use_embeddings,
    )
    if not hits:
        return CheckDecisionResult(assessment=NO_RELATED_DECISIONS)

    by_num = {d.num: d for d in decisions}
    related = [_hit_to_related(hit, by_num) for hit in hits]

    return CheckDecisionResult(
        related_decisions=related,
        assessment=_assessment(related),
    )


def _hit_to_related(hit: Bm25Hit, by_num: dict[int, Decision]) -> RelatedDecision:
    """Lift a ``bm25_retrieve`` hit into the canonical retrieval-hit shape."""
    num = hit["number"]
    decision = by_num.get(num)
    canonical_id = _canonical_decision_id(num)
    status = decision.status.value if decision else DecisionStatus.active.value
    date = decision.date.isoformat() if decision and decision.date else ""
    # Embedding-sourced hits carry similarity=None (no BM25 score); surface 0.0
    # so the score field stays a float and signals "not a BM25 match".
    similarity = hit.get("similarity")
    return RelatedDecision(
        id=canonical_id,
        title=hit.get("title", ""),
        score=similarity if similarity is not None else 0.0,
        status=status,
        date=date,
        rationale_preview=hit.get("rationale_preview", ""),
    )


def _assessment(related: list[RelatedDecision]) -> str:
    """Build the deterministic single-line assessment from retrieval facts.

    Surfaces retrieval facts — which decision ranked top, its BM25 score (or
    semantic-match origin), status, date — plus a fixed caveat that the
    ranking is lexical. It is never a confidence verdict on the match: the
    agent reads the decision body and judges; the kernel does not grade
    (no automated classification or scoring verdict).
    """
    top = related[0]
    top_num = extract_decision_number(top.id)
    top_label = _decision_label(top_num) if top_num is not None else top.id
    # score == 0.0 marks an embedding-sourced hit carrying no BM25 score (see
    # _hit_to_related). Don't label it "BM25 0.0" — it didn't match lexically.
    match_note = f"BM25 {top.score:.1f}" if top.score > 0 else "semantic match"
    top_line = (
        f'Top match: {top_label} "{top.title}"'
        f" (status {top.status}, decided {top.date}, {match_note})."
    )
    if len(related) == 1:
        target = f"get_decision({top_num})" if top_num is not None else "get_decision"
        return f"{top_line} {LEXICAL_RANK_CAVEAT} Call {target} before proposing."
    return (
        f"Found {len(related)} related decisions. {top_line} {LEXICAL_RANK_CAVEAT}"
        " Call get_decision on each related decision before proposing."
    )
