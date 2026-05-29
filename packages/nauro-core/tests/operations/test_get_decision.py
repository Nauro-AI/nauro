"""Kernel-level tests for ``operations.get_decision`` against ``InMemoryStore``.

Each test seeds an ``InMemoryStore`` and asserts on the typed
:class:`GetDecisionResult` directly. Surface-level wiring tests live in
each transport's own suite.
"""

from __future__ import annotations

from datetime import date

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    format_decision,
)
from nauro_core.operations import (
    GetDecisionResult,
    InMemoryStore,
    get_decision,
)
from nauro_core.operations.get_decision import _LEDE_MAX_CHARS


def _seed_decision(
    num: int,
    title: str,
    rationale: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
    stem: str | None = None,
) -> tuple[str, str]:
    """Return (file_stem, formatted_markdown) for a minimal v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=status,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale=rationale,
    )
    if stem is None:
        slug = title.lower().replace(" ", "-")
        stem = f"{num:03d}-{slug}"
    return stem, format_decision(decision)


def _store_with(*decisions: tuple[str, str]) -> InMemoryStore:
    return InMemoryStore(decisions=dict(decisions))


def test_returns_result_type() -> None:
    result = get_decision(InMemoryStore(), 1)
    assert isinstance(result, GetDecisionResult)


def test_empty_store_returns_not_found_error() -> None:
    result = get_decision(InMemoryStore(), 1)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert "1" in result.error.reason


def test_existing_decision_returns_full_content() -> None:
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))
    result = get_decision(store, 7)
    assert result.error is None
    assert result.content == body


def test_missing_number_returns_error_with_reason_naming_number() -> None:
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))
    result = get_decision(store, 42)
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert "42" in result.error.reason
    assert result.error.reason == "Decision 42 not found"


def test_number_resolves_from_stem_at_various_pad_widths() -> None:
    """``extract_decision_number`` accepts both unpadded and padded stems."""
    stem_5 = "5-short"
    stem_42 = "42-medium"
    stem_170 = "170-padded"
    _, body_5 = _seed_decision(5, "Short", "Test", stem=stem_5)
    _, body_42 = _seed_decision(42, "Medium", "Test", stem=stem_42)
    _, body_170 = _seed_decision(170, "Padded", "Test", stem=stem_170)
    store = _store_with((stem_5, body_5), (stem_42, body_42), (stem_170, body_170))

    for number, expected_body in ((5, body_5), (42, body_42), (170, body_170)):
        result = get_decision(store, number)
        assert result.error is None, f"unexpected miss for D{number}"
        assert result.content == expected_body


def test_superseded_decisions_still_resolve() -> None:
    """Status filtering belongs to ``list_decisions``, not ``get_decision``.

    A caller asking for a specific number must always receive the body
    when it exists — even if the decision was later superseded — so the
    rationale stays inspectable.
    """
    stem, body = _seed_decision(
        9,
        "Adopt REST endpoints",
        "Initial transport choice, later replaced by gRPC.",
        status=DecisionStatus.superseded,
    )
    store = _store_with((stem, body))
    result = get_decision(store, 9)
    assert result.error is None
    assert result.content == body


def test_store_field_absent_from_result_model_dump() -> None:
    """Transports own the ``store`` field; the kernel never emits it."""
    result = get_decision(InMemoryStore(), 1)
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# ── Backward-compat: default mode is byte-identical to the pre-change body ──


def test_default_mode_matches_full_and_verbatim_body() -> None:
    """The default call, ``mode="full"``, and the raw body all coincide.

    This is the byte-identity gate: header mode must not perturb the
    behaviour callers already depend on.
    """
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))

    default = get_decision(store, 7)
    full = get_decision(store, 7, mode="full")

    assert default.content == body
    assert full.content == body
    assert default.model_dump(mode="json") == full.model_dump(mode="json")


def test_result_model_shape_identical_across_modes() -> None:
    """No discriminator field: both modes serialize to the same key set."""
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))

    full_keys = set(get_decision(store, 7, mode="full").model_dump().keys())
    header_keys = set(get_decision(store, 7, mode="header").model_dump().keys())

    assert full_keys == header_keys == {"content", "error"}


# ── Header projection ──


def _full_decision(num: int, title: str, rationale: str, **kw) -> tuple[str, str]:
    """Seed a decision with the optional triage frontmatter fields set."""
    decision = Decision(
        date=kw.pop("date", date(2026, 5, 28)),
        confidence=kw.pop("confidence", DecisionConfidence.medium),
        status=kw.pop("status", DecisionStatus.active),
        decision_type=kw.pop("decision_type", DecisionType.api_design),
        supersedes=kw.pop("supersedes", None),
        superseded_by=kw.pop("superseded_by", None),
        rejected=kw.pop("rejected", []),
        num=num,
        title=title,
        rationale=rationale,
    )
    slug = title.lower().replace(" ", "-")
    return f"{num:03d}-{slug}", format_decision(decision)


def test_header_carries_triage_fields_title_and_lede() -> None:
    stem, body = _full_decision(
        246,
        "Header projection mode",
        "Return triage frontmatter, the title, and a short lede.\n\n"
        "A second paragraph that must not reach the header.",
        supersedes="190",
        rejected=[RejectedAlternative(name="Discriminator field", reason="speculative surface")],
    )
    store = _store_with((stem, body))
    header = get_decision(store, 246, mode="header").content

    # Triage frontmatter, in canonical order.
    assert "status: active" in header
    assert "supersedes: 190" in header
    assert "date: 2026-05-28" in header
    assert "decision_type: api_design" in header
    assert "confidence: medium" in header
    # Title line.
    assert "# 246 — Header projection mode" in header
    # Lede is the first paragraph only.
    assert "Return triage frontmatter, the title, and a short lede." in header
    assert "second paragraph" not in header


def test_header_excludes_rejected_alternatives_body() -> None:
    stem, body = _full_decision(
        12,
        "Adopt feature flags",
        "Gate risky changes behind flags.",
        rejected=[
            RejectedAlternative(
                name="Branch-by-abstraction",
                reason="heavier refactor than the change warrants",
            )
        ],
    )
    store = _store_with((stem, body))
    header = get_decision(store, 12, mode="header").content

    # The full body carries the rejected-alternatives section; the header must not.
    assert "Branch-by-abstraction" in body
    assert "Branch-by-abstraction" not in header
    assert "Rejected Alternatives" not in header


def test_header_omits_unset_frontmatter_fields() -> None:
    """A decision without optional triage fields emits only what it has."""
    stem, body = _seed_decision(3, "Minimal decision", "A one-paragraph rationale.")
    store = _store_with((stem, body))
    header = get_decision(store, 3, mode="header").content

    assert "status: active" in header
    assert "date:" in header
    assert "confidence: medium" in header
    # No supersession edges on a plain active decision.
    assert "supersedes:" not in header
    assert "superseded_by:" not in header
    # _seed_decision does not set a decision_type.
    assert "decision_type:" not in header


def test_header_lede_truncates_at_boundary() -> None:
    """A long first paragraph is capped to the lede budget with an ellipsis."""
    long_para = "word " * 200  # ~1000 chars, well past the budget
    stem, body = _full_decision(99, "Long rationale", long_para.strip())
    store = _store_with((stem, body))
    header = get_decision(store, 99, mode="header").content

    lede = header.rsplit("\n\n", 1)[-1]
    assert lede.endswith("…")
    assert len(lede) <= _LEDE_MAX_CHARS


def test_header_just_under_budget_is_not_truncated() -> None:
    """A first paragraph exactly at the budget keeps its final character."""
    para = "x" * _LEDE_MAX_CHARS
    stem, body = _full_decision(100, "At budget", para)
    store = _store_with((stem, body))
    header = get_decision(store, 100, mode="header").content

    lede = header.rsplit("\n\n", 1)[-1]
    assert lede == para
    assert "…" not in lede


def test_header_empty_decision_body_omits_lede() -> None:
    """Empty-lede guard: a supersession stub whose ``## Decision`` section
    opens with whitespace yields frontmatter + title, with no dangling lede."""
    # A supersession stub: status=superseded, empty rationale.
    decision = Decision(
        date=date(2026, 5, 5),
        confidence=DecisionConfidence.high,
        status=DecisionStatus.superseded,
        superseded_by="228",
        decision_type=DecisionType.architecture,
        num=122,
        title="Trust model superseded",
        rationale="   ",
    )
    body = format_decision(decision)
    store = _store_with(("122-trust-model-superseded", body))
    header = get_decision(store, 122, mode="header").content

    assert "status: superseded" in header
    assert "superseded_by: 228" in header
    assert header.endswith("# 122 — Trust model superseded")
    # No trailing blank block where a lede would sit.
    assert not header.endswith("\n")
    assert "\n\n\n" not in header


def test_header_not_found_returns_error() -> None:
    """Header mode shares the miss path: an absent number errors, no content."""
    stem, body = _seed_decision(7, "Adopt Redis", "Use Redis for session cache.")
    store = _store_with((stem, body))
    result = get_decision(store, 42, mode="header")
    assert result.content is None
    assert result.error is not None
    assert result.error.kind == "error"
    assert "42" in result.error.reason
