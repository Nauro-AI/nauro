"""``check_decision`` retrieves related decisions for assessment.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`CheckDecisionResult`. Each transport's adapter wraps the call to
add transport-specific framing (``store`` field and exit-code handling),
but the retrieval, ranking, and assessment text are
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
from nauro_core.operations.decision_lookup import parse_all_decisions
from nauro_core.operations.related_hits import to_related_decisions
from nauro_core.operations.results import (
    CheckDecisionResult,
    ErrorPayload,
    RelatedDecision,
)
from nauro_core.operations.store import Store
from nauro_core.parsing import _decision_label, extract_decision_number
from nauro_core.search import union_retrieve
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
    """Return related-decision retrieval and a deterministic assessment for
    ``proposed_approach``, with ``context`` concatenated into the retrieval query and
    length-bounded. ``use_embeddings`` unions an embedding pool, fail-open.
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

    related = to_related_decisions(hits, decisions)

    return CheckDecisionResult(
        related_decisions=related,
        assessment=_assessment(related),
    )


def _assessment(related: list[RelatedDecision]) -> str:
    """Build the deterministic single-line assessment from retrieval facts: top-ranked
    decision, its score or semantic-match origin, status, date, and the
    lexical-ranking caveat. It never grades the match.
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
        return (
            f"{top_line} {LEXICAL_RANK_CAVEAT} Its triage header is inline."
            f" Call {target} (mode=full) before proposing."
        )
    return (
        f"Found {len(related)} related decisions. {top_line} {LEXICAL_RANK_CAVEAT}"
        " Triage headers are inline. Call get_decision (mode=full) on each"
        " decision you reason about before proposing."
    )
