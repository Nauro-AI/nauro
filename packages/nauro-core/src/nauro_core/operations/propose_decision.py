"""``propose_decision`` — run the validation pipeline and write decisions.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP all
call this function and receive the same :class:`ProposeDecisionResult`. The
kernel owns Tier 1 structural screening, the ``operation="update"`` rejection,
``resolves_questions`` validation, Tier 2 BM25 similarity, the supersede and
question-resolve writes, and the ``touched_decisions`` enumeration.

Tier 2 hits are advisory ``similar_decisions`` on the same response and never
block the write; the human approval gate lives at the chat-session layer.
Multi-object writes are sequential and best-effort: a failure on the second
write returns a structured half-state error and leaves the first write intact.

Length validation, envelope-token rejection, ``affected_decision_id`` resolution,
snapshot capture, AGENTS.md regen, and the cloud push are adapter-side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from nauro_core.constants import (
    DECISION_HASHES_FILE,
    OPEN_QUESTIONS_MD,
)
from nauro_core.decision_model import (
    DecisionConfidence,
    DecisionSource,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
    parse_decision,
)
from nauro_core.operations._decision_transitions import (
    append_decision_update,
    attach_supersedes,
    build_new_decision,
    mark_superseded,
    resolve_questions_content,
    slugify_decision_title,
)
from nauro_core.operations._proposal_evaluation import (
    evaluate_parsed_proposal,
    validate_question_resolves,
    validate_update_metadata,
    validate_update_rationale,
)
from nauro_core.operations.decision_lookup import (
    find_decision_stem_by_id,
    find_decision_stem_by_num,
    parse_all_decisions,
)
from nauro_core.operations.results import (
    ErrorPayload,
    ProposeDecisionResult,
)
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _decision_filename,
    _decision_number_prefix,
    _decision_path,
    extract_decision_number,
)
from nauro_core.questions import OpenQuestionsFile
from nauro_core.validation import (
    compute_hash,
    rejected_item_label,
)


@dataclass(frozen=True)
class _QuestionResolveOutcome:
    """Outcome of stamping ``resolves_questions`` entries. ``resolved_ids`` are the
    requested ids whose ``resolved_by`` ended set, already-resolved targets included;
    ``relocated_ids`` and ``skipped_prose_ids`` come from the post-stamp ``normalize``
    step and feed the propose result's observability fields. Error branches return
    an empty instance.
    """

    resolved_ids: tuple[str, ...] = ()
    relocated_ids: tuple[str, ...] = ()
    skipped_prose_ids: tuple[str, ...] = ()


def propose_decision(
    store: Store,
    *,
    title: str,
    rationale: str,
    operation: Literal["add", "update", "supersede"] = "add",
    affected_decision_id: str | None = None,
    rejected: list[dict] | None = None,
    confidence: Literal["high", "medium", "low"] | None = None,
    decision_type: str | None = None,
    reversibility: Literal["easy", "moderate", "hard"] | None = None,
    files_affected: list[str] | None = None,
    resolves_questions: list[str] | None = None,
    source: str | None = None,
    base_commit: str | None = None,
) -> ProposeDecisionResult:
    """Run the proposal through the validation pipeline and commit on Tier 1 clean.
    ``confirmed`` sets ``decision_id``, ``touched_decisions``, and advisory
    ``similar_decisions``; ``rejected`` names the offending field in ``assessment``.
    """
    proposal: dict = {
        "title": title,
        "rationale": rationale,
        "rejected": rejected,
        "confidence": confidence,
        "decision_type": decision_type,
        "reversibility": reversibility,
        "files_affected": files_affected,
        "resolves_questions": list(resolves_questions) if resolves_questions else [],
        "source": source,
        "base_commit": base_commit,
    }

    requested_resolves = list(proposal.get("resolves_questions") or [])
    request_rejection = validate_update_metadata(proposal, operation=operation)
    if request_rejection is not None:
        return request_rejection
    request_rejection = validate_question_resolves(
        proposal,
        operation=operation,
        questions_file=_load_questions_file(store) if requested_resolves else None,
    )
    if request_rejection is not None:
        return request_rejection
    request_rejection = validate_update_rationale(proposal, operation=operation)
    if request_rejection is not None:
        return request_rejection

    parsed = parse_all_decisions(store)
    evaluation = evaluate_parsed_proposal(
        proposal,
        operation=operation,
        decisions=parsed,
        existing_hashes=set(_load_hash_index(store)) if operation != "update" else set(),
        affected_number=(
            extract_decision_number(affected_decision_id) if affected_decision_id else None
        ),
        enforce_claim_conflicts=True,
    )
    if isinstance(evaluation, ProposeDecisionResult):
        return evaluation

    similar_models = list(evaluation.similar_decisions)

    decision_id, actual_operation, touched, resolve_outcome, error = _execute_operation(
        store, operation, proposal, affected_decision_id
    )
    if error is not None:
        return ProposeDecisionResult(
            status="rejected",
            tier=2,
            operation="reject",
            assessment=error.reason,
            error=error,
            touched_decisions=list(touched),
            similar_decisions=similar_models,
        )

    return ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation=actual_operation,
        assessment=evaluation.assessment,
        similar_decisions=similar_models,
        decision_id=decision_id,
        touched_decisions=list(touched),
        resolved_questions=list(resolve_outcome.resolved_ids),
        relocated_ids=resolve_outcome.relocated_ids or None,
        skipped_prose_ids=resolve_outcome.skipped_prose_ids or None,
    )


def _write_decision_direct(store: Store, proposal: dict) -> str:
    """Write *proposal* as a new decision and return its file stem. Shared by the
    validated pipeline and the CLI bypass writers (``nauro note``, ``nauro import``);
    updates the hash index so later Tier 1 checks catch exact duplicates.
    """
    next_num = _next_decision_num(store)
    title = proposal.get("title", "Untitled")
    slug = slugify_decision_title(title)
    filename = f"{_decision_number_prefix(next_num)}{slug}"
    rationale = proposal.get("rationale") or title

    # base_commit rides the tolerant reader's unknown-key channel: passed as
    # an extra constructor kwarg it lands in model_extra and renders as a
    # trailing frontmatter key. It must be omitted entirely when unresolved —
    # a modeled field (or an explicit None) would serialize ``base_commit:
    # null`` into every file this writer touches.
    provenance_kwargs: dict[str, str] = {}
    base_commit = proposal.get("base_commit")
    if base_commit:
        provenance_kwargs["base_commit"] = base_commit

    decision = build_new_decision(
        number=next_num,
        decision_date=datetime.now(timezone.utc).date(),
        title=title,
        rationale=rationale,
        confidence=DecisionConfidence(
            proposal.get("confidence") or DecisionConfidence.medium.value
        ),
        decision_type=_optional_enum(proposal.get("decision_type"), DecisionType),
        reversibility=_optional_enum(proposal.get("reversibility"), Reversibility),
        source=_optional_enum(proposal.get("source"), DecisionSource),
        files_affected=_coerce_files_affected(proposal.get("files_affected")),
        rejected=_coerce_rejected(proposal.get("rejected")),
        provenance=None,
        extra_frontmatter=provenance_kwargs,
    )
    store.write_file(_decision_path(filename), format_decision(decision))

    _update_hash_index(store, title, rationale, filename)
    return filename


def _load_hash_index(store: Store) -> dict:
    body = store.read_file(DECISION_HASHES_FILE)
    if not body:
        return {}
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _save_hash_index(store: Store, index: dict) -> None:
    store.write_file(DECISION_HASHES_FILE, json.dumps(index, indent=2) + "\n")


def _update_hash_index(store: Store, title: str, rationale: str, decision_id: str) -> None:
    content_hash = compute_hash(title, rationale)
    index = _load_hash_index(store)
    index[content_hash] = {
        "decision_id": decision_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _save_hash_index(store, index)


# ── Operation execution ───────────────────────────────────────────────────


def _execute_operation(
    store: Store,
    operation: str,
    proposal: dict,
    affected_decision_id: str | None,
) -> tuple[str | None, str, tuple[str, ...], _QuestionResolveOutcome, ErrorPayload | None]:
    """Return ``(decision_id, actual_operation, touched, resolve_outcome, error)``.
    ``touched`` lists every rewritten decision stem (drives adapter AGENTS.md regen);
    half-state errors leave completed writes standing with ``resolve_outcome`` empty.
    """
    if operation == "supersede" and affected_decision_id:
        return _do_supersede(store, proposal, affected_decision_id)
    if operation == "update" and affected_decision_id:
        return _do_update(store, proposal, affected_decision_id)
    decision_id = _write_decision_direct(store, proposal)
    resolve_outcome, resolve_error = _apply_question_resolves(store, proposal, decision_id)
    return decision_id, "add", (decision_id,), resolve_outcome, resolve_error


def _do_supersede(
    store: Store,
    proposal: dict,
    affected_decision_id: str,
) -> tuple[str | None, str, tuple[str, ...], _QuestionResolveOutcome, ErrorPayload | None]:
    """Two-write supersede: new decision first, then flipped old."""
    old_num = extract_decision_number(affected_decision_id)
    if old_num is None:
        return (
            None,
            "supersede",
            (),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=f"Cannot derive supersession ref from {affected_decision_id!r}.",
            ),
        )

    # Write the new decision and rewrite it to carry the supersedes backref.
    new_decision_id = _write_decision_direct(store, proposal)
    new_body = store.read_decision(new_decision_id)
    if new_body is None:
        return (
            None,
            "supersede",
            (new_decision_id,),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=(
                    f"supersede half-state: new decision {new_decision_id} written "
                    "but the kernel cannot read it back to attach supersedes ref."
                ),
            ),
        )
    new_decision = parse_decision(new_body, _decision_filename(new_decision_id))
    new_decision_rewritten = attach_supersedes(new_decision, old_num)
    store.write_file(
        _decision_path(new_decision_id),
        format_decision(new_decision_rewritten),
    )
    new_num = new_decision.num

    # Flip the old decision. Failure here leaves the new decision standing;
    # sync-repair on next pull recovers the half-state.
    old_stem = find_decision_stem_by_num(store, old_num)
    if old_stem is None:
        return (
            new_decision_id,
            "supersede",
            (new_decision_id,),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=(
                    f"supersede half-state: new decision {new_decision_id} written; "
                    f"old decision matching {affected_decision_id!r} not found to flip."
                ),
            ),
        )
    old_body = store.read_decision(old_stem)
    if old_body is None:
        return (
            new_decision_id,
            "supersede",
            (new_decision_id,),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=(
                    f"supersede half-state: new decision {new_decision_id} written; "
                    f"old decision {old_stem} could not be read."
                ),
            ),
        )
    try:
        old_decision = parse_decision(old_body, _decision_filename(old_stem))
        old_rewritten = mark_superseded(old_decision, new_num)
        store.write_file(
            _decision_path(old_stem),
            format_decision(old_rewritten),
        )
    except Exception as exc:
        return (
            new_decision_id,
            "supersede",
            (new_decision_id,),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=(
                    f"supersede half-state: new decision {new_decision_id} written; "
                    f"old decision {old_stem} not flipped ({exc.__class__.__name__})."
                ),
            ),
        )

    resolve_outcome, resolve_error = _apply_question_resolves(store, proposal, new_decision_id)
    if resolve_error is not None:
        return (
            new_decision_id,
            "supersede",
            (new_decision_id, old_stem),
            resolve_outcome,
            resolve_error,
        )
    return new_decision_id, "supersede", (new_decision_id, old_stem), resolve_outcome, None


def _do_update(
    store: Store,
    proposal: dict,
    affected_decision_id: str,
) -> tuple[str | None, str, tuple[str, ...], _QuestionResolveOutcome, ErrorPayload | None]:
    """Rationale-only update: bump version, append dated paragraph."""
    target_stem = find_decision_stem_by_id(store, affected_decision_id)
    if target_stem is None:
        return (
            None,
            "update",
            (),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=f"update target {affected_decision_id!r} not found in store.",
            ),
        )
    body = store.read_decision(target_stem)
    if body is None:
        return (
            None,
            "update",
            (),
            _QuestionResolveOutcome(),
            ErrorPayload(
                kind="error",
                reason=f"update target {target_stem} could not be read.",
            ),
        )
    target = parse_decision(body, _decision_filename(target_stem))
    updated = append_decision_update(
        target,
        additional_rationale=proposal.get("rationale") or "",
        update_date=datetime.now(timezone.utc).date(),
        provenance=None,
    )
    store.write_file(_decision_path(target_stem), format_decision(updated))

    resolve_outcome, resolve_error = _apply_question_resolves(store, proposal, target_stem)
    if resolve_error is not None:
        return target_stem, "update", (target_stem,), resolve_outcome, resolve_error
    return target_stem, "update", (target_stem,), resolve_outcome, None


# ── resolves_questions ingestion ──────────────────────────────────────────


def _apply_question_resolves(
    store: Store,
    proposal: dict,
    decision_id: str,
) -> tuple[_QuestionResolveOutcome, ErrorPayload | None]:
    """Stamp named open questions resolved, then ``normalize`` the file layout,
    relocating prose-safe stamped entries below ``## Resolved``. The decision write
    stands even when this step faults; the error payload names the half-state.
    """
    ids = list(proposal.get("resolves_questions") or [])
    if not ids:
        return _QuestionResolveOutcome(), None
    num = extract_decision_number(decision_id)
    if num is None:
        return _QuestionResolveOutcome(), None
    try:
        content = store.read_file(OPEN_QUESTIONS_MD) or ""
        transition = resolve_questions_content(
            content,
            question_ids=ids,
            decision_number=num,
            resolved_date=datetime.now(timezone.utc).date(),
        )
        store.write_file(OPEN_QUESTIONS_MD, transition.content)
    except Exception as exc:
        return _QuestionResolveOutcome(), ErrorPayload(
            kind="error",
            reason=(
                f"question-resolution half-state: decision {decision_id} written; "
                f"open-questions.md not updated ({exc.__class__.__name__})."
            ),
        )
    return (
        _QuestionResolveOutcome(
            resolved_ids=transition.resolved_ids,
            relocated_ids=transition.relocated_ids,
            skipped_prose_ids=transition.skipped_prose_ids,
        ),
        None,
    )


def _load_questions_file(store: Store) -> OpenQuestionsFile | None:
    content = store.read_file(OPEN_QUESTIONS_MD)
    if content is None:
        return None
    return OpenQuestionsFile.parse(content)


# ── Decision write plumbing ───────────────────────────────────────────────


def _next_decision_num(store: Store) -> int:
    """Return ``max(existing num) + 1`` over decisions in the store."""
    nums: list[int] = []
    for stem in store.list_decisions():
        n = extract_decision_number(stem)
        if n is not None:
            nums.append(n)
    return max(nums, default=0) + 1


def _optional_enum(raw, enum_cls):
    if raw is None:
        return None
    if isinstance(raw, enum_cls):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    return enum_cls(s)


def _coerce_files_affected(files_affected) -> list[str]:
    if files_affected is None:
        return []
    if isinstance(files_affected, str):
        try:
            decoded = json.loads(files_affected)
            if isinstance(decoded, list):
                return [str(x) for x in decoded]
            return [files_affected]
        except (json.JSONDecodeError, ValueError):
            return [files_affected]
    return list(files_affected)


def _coerce_rejected(rejected) -> list[RejectedAlternative]:
    if rejected is None:
        return []
    if isinstance(rejected, str):
        try:
            rejected = json.loads(rejected)
        except (json.JSONDecodeError, ValueError):
            return []
    if not rejected:
        return []
    out: list[RejectedAlternative] = []
    for item in rejected:
        if isinstance(item, RejectedAlternative):
            out.append(item)
        elif isinstance(item, dict):
            # Tier 1 already rejects nameless items on the validated path;
            # raising here is fail-loud insurance for bypass callers rather
            # than silently defaulting the heading.
            name = rejected_item_label(item)
            if name is None:
                raise ValueError(
                    "rejected item has no label: expected a non-empty "
                    f"'alternative' (or 'name') key; got keys {list(item.keys())}."
                )
            reason = item.get("reason")
            out.append(RejectedAlternative(name=name, reason=reason or None))
        elif isinstance(item, str):
            out.append(RejectedAlternative(name=item, reason=None))
    return out


__all__ = [
    "propose_decision",
    "ProposeDecisionResult",
    "_write_decision_direct",
]
