"""Kernel-level tests for ``operations.propose_decision``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` so the
Tier 1 / Tier 2 plumbing, multi-object write, and ``resolves_questions``
ingestion exercise the locked Store protocol without any filesystem
dependency. Surface-level wiring (snapshot capture, push hooks, length
validation, envelope-token rejection, ``affected_decision_id``
resolution, AGENTS.md regen) lives in the consumer package; the kernel
must never reach into those primitives.

The single-call flow commits on Tier 1 clean; Tier 2 BM25 hits surface
as advisory ``similar_decisions`` on the same response rather than
gating the write.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.decision_model import (
    DECISION_TYPE_VALUES,
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.operations import (
    InMemoryStore,
    ProposeDecisionResult,
    propose_decision,
)


def _seed_decision(
    num: int,
    title: str,
    rationale: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
    decision_date: date | None = None,
) -> tuple[str, str]:
    """Return ``(file_stem, formatted_markdown)`` for a parseable v2 decision."""
    superseded_by = "999" if status is DecisionStatus.superseded else None
    decision = Decision(
        date=decision_date or date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=status,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale=rationale,
    )
    slug = title.lower().replace(" ", "-")
    stem = f"{num:03d}-{slug}"
    return stem, format_decision(decision)


def _store_with(*decisions: tuple[str, str], **files: str) -> InMemoryStore:
    return InMemoryStore(decisions=dict(decisions), files=dict(files))


# ── Result type / shape ─────────────────────────────────────────────────


def test_returns_result_type() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    assert isinstance(result, ProposeDecisionResult)


def test_result_is_frozen() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    with pytest.raises(ValidationError):
        result.status = "rejected"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProposeDecisionResult(
            status="confirmed",
            tier=2,
            operation="add",
            unexpected="value",
        )


def test_result_exclude_none_strips_unset_fields() -> None:
    """``status="confirmed"`` paths still carry empty list defaults; the
    transport adapter pops them. The result model itself only drops
    ``None`` values via ``exclude_none``."""
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert "error" not in dumped
    assert dumped["status"] == "confirmed"


def test_store_field_absent_from_result_model_dump() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for hot read paths across the API tier.",
        confidence="medium",
    )
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# ── add: auto-confirm path ──────────────────────────────────────────────


def test_add_empty_store_auto_confirms_and_writes_decision() -> None:
    store = InMemoryStore()
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.decision_id is not None
    assert result.touched_decisions == [result.decision_id]
    # The decision file was written and is readable back through the
    # decision view.
    body = store.read_decision(result.decision_id)
    assert body is not None
    assert "Adopt Redis for hot caching" in body


def test_add_auto_confirm_branch_when_unrelated_decisions_exist() -> None:
    """A seeded decision unrelated to the proposal still hits the
    Tier 2 auto_confirm branch."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="Add dark mode toggle to settings page",
        rationale="Users have requested a dark theme for reduced eye strain.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.decision_id is not None


# ── Tier 2 advisory similar_decisions ──────────────────────────────────


class TestAdvisorySimilarDecisions:
    """Tier 2 BM25 hits surface as advisory ``similar_decisions`` on the
    same response. The write still commits — the human approval gate
    lives at the chat-session layer, not in the kernel."""

    def test_similar_decision_still_confirms_and_surfaces_advisory_hits(self) -> None:
        store = _store_with(
            _seed_decision(
                1,
                "Adopt PostgreSQL primary database",
                "Mature ecosystem with strong JSON support and excellent tooling.",
            ),
        )
        result = propose_decision(
            store,
            title="Use PostgreSQL for the data layer",
            rationale="Better JSON handling than alternatives for our application data.",
            confidence="medium",
        )
        # The write committed on the same call — no pending state.
        assert result.status == "confirmed"
        assert result.tier == 2
        assert result.operation == "add"
        assert result.decision_id is not None
        # The Tier 2 hit rides along as advisory context.
        assert len(result.similar_decisions) >= 1
        # The decision file landed on disk despite the similarity hit.
        assert result.decision_id in store.list_decisions()
        assert "similar" in result.assessment.lower()

    def test_no_similar_decision_returns_empty_advisory_list(self) -> None:
        store = _store_with(
            _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
        )
        result = propose_decision(
            store,
            title="Add dark mode toggle to settings page",
            rationale="Users have requested a dark theme for reduced eye strain.",
            confidence="medium",
        )
        assert result.status == "confirmed"
        assert result.similar_decisions == []


# ── corpus-scan deduplication ───────────────────────────────────────────


class _ScanCountingStore(InMemoryStore):
    """In-memory store that counts full-corpus scans.

    ``parse_all_decisions`` is the expensive scan being deduplicated: it
    lists every decision stem and then ``read_decision``\\ s each one to parse
    it. We count corpus scans by counting ``read_decision`` calls and dividing
    by the seeded corpus size, because ``list_decisions`` alone conflates a
    corpus parse with the cheap stem-only listing in ``_next_decision_num``
    on the write path. ``corpus_scans`` therefore isolates the Tier 1 / Tier 2
    parse work the change collapses from two scans to one.
    """

    def __init__(
        self,
        decisions: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        super().__init__(decisions=decisions, files=files)
        self.read_decision_calls = 0

    def read_decision(self, file_stem: str) -> str | None:
        self.read_decision_calls += 1
        return super().read_decision(file_stem)

    def corpus_scans(self, corpus_size: int) -> int:
        """Number of full-corpus parses, derived from per-stem reads."""
        if corpus_size == 0:
            return 0
        scanned, remainder = divmod(self.read_decision_calls, corpus_size)
        assert remainder == 0, (
            f"read_decision called {self.read_decision_calls} times for a "
            f"corpus of {corpus_size}; not a whole number of full scans."
        )
        return scanned


def test_add_accept_scans_corpus_once() -> None:
    """A non-update ``add`` accept parses the corpus exactly once: Tier 1 and
    Tier 2 share the single parsed list rather than re-reading and re-parsing
    the store. Before the change this path parsed the corpus twice."""
    store = _ScanCountingStore(
        decisions=dict(
            (
                _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
                _seed_decision(2, "Adopt Redis", "In-memory cache for hot read paths."),
            )
        )
    )
    result = propose_decision(
        store,
        title="Add dark mode toggle to settings page",
        rationale="Users have requested a dark theme for reduced eye strain.",
        confidence="medium",
    )
    assert store.corpus_scans(corpus_size=2) == 1
    # The accept path is unchanged: same status, tier, decision id, and
    # advisory shape as the existing auto-confirm expectations.
    assert result.status == "confirmed"
    assert result.tier == 2
    assert result.operation == "add"
    assert result.decision_id is not None
    assert result.similar_decisions == []


def test_update_short_rationale_rejects_without_scanning() -> None:
    """An ``operation="update"`` with a too-short rationale rejects at Tier 1
    before any corpus scan. Guards the lazy-compute: ``parsed`` must not be
    hoisted to the top, which would regress this path to a needless scan."""
    store = _ScanCountingStore(
        decisions=dict((_seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),))
    )
    result = propose_decision(
        store,
        title="",
        rationale="too short",
        operation="update",
        affected_decision_id="decision-001",
    )
    assert store.corpus_scans(corpus_size=1) == 0
    assert result.status == "rejected"
    assert result.tier == 1


# ── supersede ───────────────────────────────────────────────────────────


def test_supersede_writes_both_files_and_flips_frontmatter() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="Switch to managed PostgreSQL provider",
        rationale="Reduces operational burden; the self-hosting rationale no longer applies.",
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    # The kernel commits the supersede write on the same call. Tier 2
    # advisory similar_decisions may ride along; the write stands.
    assert result.status == "confirmed"
    assert result.operation == "supersede"
    assert result.decision_id is not None
    assert set(result.touched_decisions) >= {
        result.decision_id,
        "001-adopt-postgresql-primary-database",
    }
    new_body = store.read_decision(result.decision_id)
    assert new_body is not None
    assert "supersedes:" in new_body
    old_body = store.read_decision("001-adopt-postgresql-primary-database")
    assert old_body is not None
    assert "status: superseded" in old_body
    assert "superseded_by:" in old_body


def test_supersede_no_similarity_still_executes_write() -> None:
    """``operation="supersede"`` with no Tier 2 hit still performs the
    multi-object write on the same call."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="Switch to managed dark-mode-only frontend",
        rationale=(
            "Users have requested a permanent dark theme; this "
            "supersedes a totally different choice."
        ),
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    assert result.status == "confirmed"
    assert result.operation == "supersede"
    assert set(result.touched_decisions) == {
        result.decision_id,
        "001-adopt-postgresql",
    }
    new_body = store.read_decision(result.decision_id)
    assert new_body is not None
    assert "supersedes: '1'" in new_body
    old_body = store.read_decision("001-adopt-postgresql")
    assert old_body is not None
    assert "status: superseded" in old_body


# ── active-title dedup (Tier 1) ──────────────────────────────────────────


def test_add_title_matching_old_active_decision_rejects() -> None:
    """An add whose title matches an active decision rejects regardless of the
    matched decision's age. Under the prior 24h window this add would have
    wrongly passed because the existing decision fell outside the window."""
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
            decision_date=date(2024, 1, 1),
        ),
    )
    result = propose_decision(
        store,
        title="Adopt PostgreSQL primary database",
        rationale="Re-proposing the same choice with fresh rationale text that is long enough.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "active decision already has this title" in result.assessment.lower()
    assert "D1" in result.assessment


def test_add_title_matching_superseded_decision_passes() -> None:
    """Title dedup keys on active status, so an add whose title matches a
    superseded decision is not blocked."""
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
            status=DecisionStatus.superseded,
            decision_date=date(2024, 1, 1),
        ),
    )
    result = propose_decision(
        store,
        title="Adopt PostgreSQL primary database",
        rationale="The superseded decision no longer applies; re-establishing the choice now.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.operation == "add"
    assert result.decision_id is not None


def test_supersede_same_title_as_target_confirms() -> None:
    """A supersede whose new title equals the target's own title must not
    self-reject: screening runs before the flip, so the still-active target is
    excluded from the dedup set."""
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
            decision_date=date(2024, 1, 1),
        ),
    )
    result = propose_decision(
        store,
        title="Adopt PostgreSQL primary database",
        rationale="Keeping the title but recording a materially revised rationale for the choice.",
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    assert result.status == "confirmed"
    assert result.operation == "supersede"
    assert result.decision_id is not None


def test_supersede_title_collides_with_different_active_decision_rejects() -> None:
    """A supersede whose new title collides with a *different* active decision
    still rejects — only the supersede target is excluded from the dedup set."""
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
        _seed_decision(
            2,
            "Adopt Redis for hot caching",
            "In-memory cache keeps hot read paths off the primary database.",
        ),
    )
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="Superseding decision 1 but reusing a title already held by active decision 2.",
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "active decision already has this title" in result.assessment.lower()
    assert "D2" in result.assessment


def test_add_matching_active_scaffold_seed_rejects() -> None:
    """The scaffold seed is an active decision, so an add titled exactly as the
    seed rejects on the active-title axis. Tier 2 seed filtering is unrelated:
    Tier 1 rejects before Tier 2 runs."""
    store = _store_with(
        _seed_decision(
            1,
            "Initial project setup",
            "Scaffold-seeded bookkeeping decision recording store initialization.",
        ),
    )
    result = propose_decision(
        store,
        title="Initial project setup",
        rationale="A second add reusing the scaffold seed title that should be turned away.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "active decision already has this title" in result.assessment.lower()
    assert "D1" in result.assessment


# ── update ──────────────────────────────────────────────────────────────


def test_update_appends_rationale_paragraph() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="",
        rationale="Adds a managed-extensions clause to the existing PostgreSQL choice.",
        operation="update",
        affected_decision_id="decision-001",
    )
    # The kernel commits the rationale append on the same call.
    assert result.status == "confirmed"
    assert result.operation == "update"
    assert result.decision_id == "001-adopt-postgresql"
    assert result.touched_decisions == ["001-adopt-postgresql"]
    body = store.read_decision("001-adopt-postgresql")
    assert body is not None
    assert "managed-extensions clause" in body
    # The version frontmatter incremented.
    assert "version: 2" in body


def test_update_rationale_only_omitting_title_preserves_target_title() -> None:
    """A rationale-only update that omits ``title`` confirms and leaves the
    target's title untouched.

    This is the kernel half of the deadlock fix: a schema-respecting client now
    omits ``title`` entirely (rather than being forced to send a non-empty value
    the kernel would reject). ``title=None`` must pass the disallowed-fields
    check and the rewrite must preserve the original title verbatim.
    """
    original_title = "Adopt PostgreSQL"
    store = _store_with(
        _seed_decision(
            1,
            original_title,
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title=None,
        rationale="Add a managed-extensions clause after the first month in production.",
        operation="update",
        affected_decision_id="decision-001",
    )
    assert result.status == "confirmed"
    assert result.operation == "update"
    body = store.read_decision("001-adopt-postgresql")
    assert body is not None
    # The title lives in the decision heading; the update must leave it intact.
    assert f"— {original_title}" in body
    assert "managed-extensions clause" in body


def test_update_disallowed_field_rejected_loudly() -> None:
    """``operation="update"`` cannot change metadata; rejection at Tier 0."""
    store = _store_with(
        _seed_decision(1, "Adopt PostgreSQL", "ACID transactional semantics."),
    )
    result = propose_decision(
        store,
        title="A new title",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        operation="update",
        affected_decision_id="decision-001",
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert result.operation == "update"
    assert "title" in result.assessment


# ── Tier 1 structural rejection ─────────────────────────────────────────


def test_add_empty_title_rejected_at_tier_1() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "title" in result.assessment.lower()


def test_add_short_rationale_rejected_at_tier_1() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="too short",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.tier == 1


def test_add_rejected_item_without_label_rejected_at_tier_1() -> None:
    """A dict-form rejected item with no 'alternative'/'name' label rejects
    at Tier 1 instead of silently defaulting the heading, and nothing is
    written to the store."""
    store = InMemoryStore()
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        rejected=[{"title": "Memcached", "reason": "No native persistence."}],
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert result.operation == "reject"
    assert "rejected[0] has no label" in result.assessment
    assert "'alternative'" in result.assessment
    assert store.list_decisions() == []


def test_add_valid_rejected_writes_labeled_headings() -> None:
    store = InMemoryStore()
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        rejected=[{"alternative": "Memcached", "reason": "No native persistence."}],
    )
    assert result.status == "confirmed"
    body = store.read_decision(result.decision_id)
    assert body is not None
    assert "## Rejected Alternatives" in body
    assert "### Memcached" in body


def test_supersede_rejected_item_without_label_rejects_before_write() -> None:
    store = _store_with(
        _seed_decision(
            1,
            "Adopt PostgreSQL primary database",
            "Mature ecosystem with strong JSON support and excellent tooling.",
        ),
    )
    result = propose_decision(
        store,
        title="Switch to managed PostgreSQL provider",
        rationale="Reduces operational burden; the self-hosting rationale no longer applies.",
        confidence="medium",
        operation="supersede",
        affected_decision_id="decision-001",
        rejected=[{"reason": "No label on this one."}],
    )
    assert result.status == "rejected"
    assert result.tier == 1
    assert "rejected[0] has no label" in result.assessment
    # No new decision written; the supersede target is untouched.
    assert store.list_decisions() == ["001-adopt-postgresql-primary-database"]
    old_body = store.read_decision("001-adopt-postgresql-primary-database")
    assert old_body is not None
    assert "status: active" in old_body


# ── resolves_questions ──────────────────────────────────────────────────


def test_resolves_questions_relocates_entry_below_divider() -> None:
    """A confirmed add with ``resolves_questions=["Q1"]`` writes the decision
    file, stamps Q1 with the decision ref, and — the entry being prose-safe —
    relocates it below the ## Resolved divider (self-heal on resolve)."""
    open_questions = (
        "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    assert result.status == "confirmed"
    assert result.decision_id is not None
    assert result.resolved_questions == ["Q1"]
    assert result.relocated_ids == ("Q1",)
    assert result.skipped_prose_ids is None
    updated = store.read_file(OPEN_QUESTIONS_MD)
    assert updated is not None
    # The entry now sits below the ## Resolved divider, carrying the ref.
    assert "[Q1] Should we adopt PostgreSQL?" in updated
    assert updated.index("## Resolved") < updated.index("[Q1]")
    # Pull the leading number off the decision id stem to confirm the ref points to it.
    decision_num = int(result.decision_id.split("-", 1)[0])
    assert f"D{decision_num}" in updated or f"decision-{decision_num:03d}" in updated


def test_resolves_questions_heals_pre_existing_stray_whole_file_scope() -> None:
    """Whole-file scope: resolving Q1 also relocates a pre-existing stray Q9
    that was stamped resolved earlier but never moved below the divider."""
    open_questions = (
        "# Open Questions\n"
        "\n"
        "## Active\n"
        "\n"
        "- [Q1] Should we adopt PostgreSQL?\n"
        "- [Resolved by D40 on 2026-05-01] [Q9] pre-existing stray above divider\n"
        "\n"
        "## Resolved\n"
        "\n"
        "- [Resolved by D30 on 2026-04-01] [Q3] properly resolved earlier\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    assert result.status == "confirmed"
    assert result.resolved_questions == ["Q1"]
    # Both the freshly-stamped Q1 and the pre-existing Q9 stray move down.
    assert result.relocated_ids == ("Q1", "Q9")
    updated = store.read_file(OPEN_QUESTIONS_MD)
    assert updated is not None
    divider_at = updated.index("## Resolved")
    assert divider_at < updated.index("[Q1]")
    assert divider_at < updated.index("[Q9]")


def test_resolves_questions_unknown_id_rejected_at_boundary() -> None:
    open_questions = "# Open Questions\n\n## Active\n\n- [Q1] Real question?\n\n## Resolved\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        resolves_questions=["Q999"],
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert "unknown" in result.assessment.lower()
    assert "Q999" in result.assessment
    # No decision file written on rejection.
    assert store.list_decisions() == []


def test_resolves_questions_ambiguous_id_rejected_at_boundary() -> None:
    """When two entries collide on the same id, the boundary surfaces the
    counterpart suggestions."""
    open_questions = (
        "# Open Questions\n\n## Active\n\n"
        "- [Q1] First duplicate (num=1)\n"
        "- [Q1] Second duplicate (num=2)\n\n## Resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    assert result.status == "rejected"
    assert result.tier == 0
    assert "ambiguous" in result.assessment.lower()
    assert "Q1" in result.assessment


def test_add_resolves_questions_half_state_surfaces_error() -> None:
    """``add`` half-state: decision file written, open-questions.md write
    fails. The error must propagate through the result envelope so the
    caller learns the questions file is out of sync.

    Parity with the supersede / update error paths: when the multi-object
    write hits a mid-sequence failure, the envelope carries the error
    payload naming the half-state.
    """
    open_questions = (
        "# Open Questions\n\n## Active\n\n- [Q1] Should we adopt PostgreSQL?\n\n## Resolved\n"
    )

    class _OpenQuestionsWriteFails(InMemoryStore):
        def write_file(self, path: str, content: str) -> None:
            if path == OPEN_QUESTIONS_MD:
                raise OSError("simulated open-questions.md write failure")
            super().write_file(path, content)

    store = _OpenQuestionsWriteFails(files={OPEN_QUESTIONS_MD: open_questions})
    result = propose_decision(
        store,
        title="Adopt PostgreSQL for the data layer",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        confidence="medium",
        resolves_questions=["Q1"],
    )
    # The half-state error surfaces as a rejected envelope with the
    # written decision id in touched_decisions; the kernel does not
    # silently drop the resolve failure.
    assert result.status == "rejected"
    assert result.tier == 2
    assert result.error is not None
    assert result.error.kind == "error"
    assert "question-resolution half-state" in result.error.reason
    assert result.resolved_questions == []
    # The decision file was written before the open-questions write
    # failed; touched_decisions surfaces it so sync-repair can reconcile.
    assert len(result.touched_decisions) == 1
    decision_id = result.touched_decisions[0]
    assert store.read_decision(decision_id) is not None


# ── touched_decisions enumeration ───────────────────────────────────────


def test_touched_decisions_only_new_id_on_add() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="Adopt Redis",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.touched_decisions == [result.decision_id]


def test_touched_decisions_empty_on_rejection() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="medium",
    )
    assert result.status == "rejected"
    assert result.touched_decisions == []


# ── Import-graph negative constraint ────────────────────────────────────


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "propose_decision.py"
    )
    tree = ast.parse(module_path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add(f"{module}.{alias.name}")
                imported.add(alias.name)
                imported.add(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    return imported


def test_kernel_does_not_import_adapter_or_path_primitives() -> None:
    imported = _kernel_imports()
    forbidden_names = {
        "nauro.store.snapshot",
        "nauro.sync.hooks",
        "nauro.validation.pending",
        "nauro.validation.pipeline",
        "nauro.validation.tier1",
        "nauro.validation.tier2",
        "capture_snapshot",
        "push_after_write",
        "pull_before_session",
    }
    leaked = forbidden_names & imported
    assert not leaked, (
        f"kernel imports adapter-side primitives: {leaked}; "
        "those live on the transport, not in the kernel."
    )
    assert "pathlib" not in imported and "pathlib.Path" not in imported, (
        "kernel imports pathlib; the Store protocol abstracts paths as strings."
    )
    assert "filelock" not in imported, "kernel must not import filelock"


# ── _write_decision_direct helper ───────────────────────────────────────


def test_write_decision_direct_writes_file_and_updates_hash_index() -> None:
    from nauro_core.constants import DECISION_HASHES_FILE
    from nauro_core.operations.propose_decision import _write_decision_direct

    store = InMemoryStore()
    decision_id = _write_decision_direct(
        store,
        {
            "title": "Adopt Redis",
            "rationale": "In-memory cache for the hot read paths across the API tier.",
            "confidence": "high",
        },
    )
    assert decision_id.startswith("001-")
    body = store.read_decision(decision_id)
    assert body is not None
    assert "Adopt Redis" in body
    # The hash index records the new decision.
    hash_body = store.read_file(DECISION_HASHES_FILE)
    assert hash_body is not None
    assert decision_id in hash_body


def test_write_decision_direct_increments_decision_number() -> None:
    from nauro_core.operations.propose_decision import _write_decision_direct

    store = _store_with(
        _seed_decision(1, "First", "First rationale that comfortably exceeds the minimum length."),
        _seed_decision(2, "Second", "Second rationale that also comfortably exceeds the minimum."),
    )
    decision_id = _write_decision_direct(
        store,
        {
            "title": "Third decision in line",
            "rationale": "Third rationale that comfortably exceeds the minimum length too.",
            "confidence": "medium",
        },
    )
    assert decision_id.startswith("003-")


@pytest.mark.parametrize("decision_type", list(DECISION_TYPE_VALUES))
def test_every_advertised_decision_type_commits(decision_type: str) -> None:
    """Every decision_type the schema advertises must reach the validator
    intact and commit. This is the end-to-end guard that was missing when the
    advertised list drifted from the DecisionType enum: an advertised value
    that the enum does not accept raised an uncaught ValueError on the write
    path. Driving propose_decision over the canonical value set keeps the two
    in lockstep."""
    result = propose_decision(
        InMemoryStore(),
        title=f"Decision typed as {decision_type}",
        rationale="A rationale long enough to clear the minimum length threshold.",
        confidence="medium",
        decision_type=decision_type,
    )
    assert result.status == "confirmed"


# ── Slug truncation goldens ─────────────────────────────────────────────


def test_slug_early_dash_no_longer_collapses_slug() -> None:
    # The word-boundary backoff after truncation had no floor: a title whose
    # only dash sits early collapsed to a near-empty slug ("001-aaaa"). The
    # backoff now applies only while it keeps at least half the cap;
    # otherwise the hard character cut stands.
    result = propose_decision(
        InMemoryStore(),
        title=f"Aaaa {'b' * 80}",
        rationale="A rationale long enough to clear the minimum length threshold.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.decision_id == "001-aaaa-" + "b" * 55


def test_slug_multiword_title_still_trims_at_word_boundary() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="word " * 20,
        rationale="A rationale long enough to clear the minimum length threshold.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.decision_id == "001-" + "word-" * 11 + "word"


def test_slug_single_giant_word_hard_cuts_at_cap() -> None:
    result = propose_decision(
        InMemoryStore(),
        title="x" * 80,
        rationale="A rationale long enough to clear the minimum length threshold.",
        confidence="medium",
    )
    assert result.status == "confirmed"
    assert result.decision_id == "001-" + "x" * 60
