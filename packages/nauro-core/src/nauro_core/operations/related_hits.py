"""Shared triage projection for retrieval hits and decision headers.

``check_decision`` and ``propose_decision`` both lift raw BM25 hit dicts
into the canonical :class:`RelatedDecision` shape, and ``get_decision``'s
header mode projects the same triage fields for a single decision. This
module owns the shared primitives — the lede budget, the first-paragraph
lede extraction, the frontmatter serialization rules, and the hit lifting
itself — so the enriched hit and the header projection cannot drift.
"""

from __future__ import annotations

from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.operations.results import RelatedDecision
from nauro_core.parsing import _canonical_decision_id
from nauro_core.search import Bm25Hit

# Lede budget. The first paragraph of the rationale is the load-bearing
# sentence(s); cap it so triage projections stay compact while still
# carrying enough rationale to triage on.
LEDE_MAX_CHARS = 300


def decision_lede(rationale: str) -> str:
    """Return the first paragraph of the rationale, truncated to the budget.

    ``""`` for an empty or whitespace-only rationale, so triage omits the lede block.
    """
    stripped = rationale.strip()
    if not stripped:
        return ""
    paragraph = stripped.split("\n\n", 1)[0].strip()
    if not paragraph:
        return ""
    if len(paragraph) <= LEDE_MAX_CHARS:
        return paragraph
    return paragraph[: LEDE_MAX_CHARS - 1].rstrip() + "…"


def frontmatter_value(decision: Decision, field: str) -> str:
    """Return the on-disk string for a triage frontmatter field, or ``""``.

    Enum fields serialize to their token and ``date`` to ISO; refs are plain integers.
    """
    raw = getattr(decision, field, None)
    if raw is None:
        return ""
    if field == "date":
        return raw.isoformat()
    value = getattr(raw, "value", raw)
    return str(value)


def to_related_decisions(
    hits: list[Bm25Hit],
    decisions: list[Decision],
) -> list[RelatedDecision]:
    """Lift ``bm25_retrieve``/``union_retrieve`` hits into :class:`RelatedDecision`, the
    one path shared by ``check_decision`` and ``propose_decision``. Enrichment reads
    the parsed corpus the caller already holds, so it costs no extra I/O.
    """
    by_num: dict[int, Decision] = {d.num: d for d in decisions}
    return [_hit_to_related(hit, by_num) for hit in hits]


def _hit_to_related(hit: Bm25Hit, by_num: dict[int, Decision]) -> RelatedDecision:
    num = hit["number"]
    decision = by_num.get(num)
    status = decision.status.value if decision else DecisionStatus.active.value
    date = decision.date.isoformat() if decision and decision.date else ""
    # The preview carries the same lede the header projection shows: first
    # paragraph of the rationale under the shared budget. Fall back to the
    # raw hit preview only on the defensive miss (hit without a parsed
    # decision, e.g. a backend racing a delete against enumeration).
    preview = decision_lede(decision.rationale) if decision else hit.get("rationale_preview", "")
    # Embedding-sourced hits carry similarity=None (no BM25 score); surface 0.0
    # so the score field stays a float and signals "not a BM25 match".
    similarity = hit.get("similarity")
    return RelatedDecision(
        id=_canonical_decision_id(num),
        title=hit.get("title", ""),
        score=similarity if similarity is not None else 0.0,
        status=status,
        date=date,
        rationale_preview=preview,
        decision_type=_optional_frontmatter(decision, "decision_type"),
        confidence=_optional_frontmatter(decision, "confidence"),
        supersedes=_optional_frontmatter(decision, "supersedes"),
        superseded_by=_optional_frontmatter(decision, "superseded_by"),
    )


def _optional_frontmatter(decision: Decision | None, field: str) -> str | None:
    """Frontmatter string for enrichment fields; ``None`` when absent.

    ``None`` rather than ``""`` keeps unset fields out of ``exclude_none=True`` dumps.
    """
    if decision is None:
        return None
    return frontmatter_value(decision, field) or None
