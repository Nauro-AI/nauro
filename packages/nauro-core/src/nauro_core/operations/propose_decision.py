"""``propose_decision`` — run the validation pipeline and write decisions.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`ProposeDecisionResult`. The kernel owns:

* Tier 1 structural screening (rejects empty fields, short rationale,
  exact-hash duplicates, and titles that match a decision still in force).
* ``operation="update"`` disallowed-fields rejection.
* ``resolves_questions`` boundary validation (unknown ids, ambiguous
  ids).
* Tier 2 BM25 similarity over the in-store decision corpus. Hits surface
  as advisory ``similar_decisions`` on the same response; they do not
  block the write. The human approval gate is enforced at the
  chat-session layer before the agent fires this call.
* Multi-object writes on supersede (new decision then flipped old) and
  ``resolves_questions`` ingestion. The writes are sequential and
  best-effort: a failure on the second write returns a structured
  half-state error and leaves the first write intact so sync-repair can
  reconcile on the next pull.
* ``touched_decisions`` enumeration so the adapter knows which files to
  regenerate AGENTS.md against.

Length validation, envelope-token rejection, ``affected_decision_id``
resolution, snapshot capture, AGENTS.md regen, and the best-effort cloud
push stay on the adapter side per the locked Store Protocol boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from nauro_core.constants import (
    DECISION_HASHES_FILE,
    MIN_RATIONALE_LENGTH,
    OPEN_QUESTIONS_MD,
)
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
    parse_decision,
)
from nauro_core.operations.decision_lookup import (
    find_decision_stem_by_id,
    find_decision_stem_by_num,
    parse_all_decisions,
)
from nauro_core.operations.results import (
    ErrorPayload,
    ProposeDecisionResult,
    RelatedDecision,
)
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _canonical_decision_id,
    _decision_filename,
    _decision_number_prefix,
    _decision_path,
    extract_decision_number,
)
from nauro_core.questions import EntryBlock, OpenQuestionsFile
from nauro_core.search import Bm25Hit
from nauro_core.validation import (
    check_bm25_similarity,
    compute_hash,
    rejected_item_label,
    screen_structural,
)

# operation="update" appends rationale only — update_decision reads only
# affected_decision_id + rationale. Any value in these fields would be
# silently dropped on local stdio (and rejected on remote MCP); reject
# loudly at the boundary so the wording in PROPOSE_DECISION_OPERATIONS holds
# on both transports.
_UPDATE_DISALLOWED_FIELDS: tuple[str, ...] = (
    "title",
    "rejected",
    "files_affected",
    "decision_type",
    "reversibility",
    "confidence",
)

_SLUG_MAX_LENGTH = 60


@dataclass(frozen=True)
class _QuestionResolveOutcome:
    """Result of stamping (and self-healing) ``resolves_questions`` entries.

    ``resolved_ids`` are the ids whose ``resolved_by`` ended set;
    ``relocated_ids`` / ``skipped_prose_ids`` come from the post-stamp
    ``normalize`` step and feed the propose result's observability fields.
    Internal to the propose write path — carried in the execute tuple so the
    error branches (which never relocate) can return an empty instance.
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
) -> ProposeDecisionResult:
    """Run the proposal through the validation pipeline and commit on Tier 1 clean.

    Returns:
        :class:`ProposeDecisionResult` with ``status`` of ``confirmed`` or
        ``rejected``. On the confirmed path ``decision_id`` and
        ``touched_decisions`` are set; ``similar_decisions`` carries any
        Tier 2 BM25 advisory hits for the agent to surface alongside the
        write. On the rejected path ``assessment`` names the reason and
        ``error`` carries the structured payload.
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
    }

    # --- operation="update": reject metadata fields up front ---
    if operation == "update":
        disallowed = [
            name
            for name in _UPDATE_DISALLOWED_FIELDS
            if proposal.get(name)
            and (not isinstance(proposal.get(name), str) or proposal.get(name).strip())
        ]
        if disallowed:
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation="update",
                assessment=(
                    'operation="update" appends rationale only; cannot change '
                    f"{', '.join(disallowed)}. "
                    'Use operation="supersede" to replace the decision with new metadata.'
                ),
            )

    # --- resolves_questions: unknown / ambiguous ids reject at boundary ---
    requested_resolves = list(proposal.get("resolves_questions") or [])
    if requested_resolves:
        questions_file = _load_questions_file(store)
        unknown = _unknown_question_ids(requested_resolves, questions_file)
        if unknown:
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation=operation,
                assessment=(
                    "resolves_questions contains unknown id(s): "
                    + ", ".join(repr(x) for x in unknown)
                    + ". Call get_context (L0 lists every open question) to "
                    "see the canonical ids in open-questions.md."
                ),
            )
        ambiguous = _ambiguous_question_ids(requested_resolves, questions_file)
        if ambiguous:
            offenders = "; ".join(
                f"{requested!r} matches {len(counterparts)} entries — disambiguate "
                f"to one of: {', '.join(counterparts)}"
                for requested, counterparts in ambiguous.items()
            )
            return ProposeDecisionResult(
                status="rejected",
                tier=0,
                operation=operation,
                assessment="resolves_questions contains ambiguous id(s): " + offenders + ".",
            )

    # --- Tier 1: structural screening ---
    # ``parsed`` is the single corpus scan shared between Tier 1 active-title
    # dedup and Tier 2 BM25. It stays None on the update early-reject path so a
    # short-rationale update rejects without scanning the corpus at all.
    parsed: list[Decision] | None = None
    if operation == "update":
        rationale_stripped = (proposal.get("rationale") or "").strip()
        if not rationale_stripped:
            action, reason = "reject", "Rationale is empty."
        elif len(rationale_stripped) < MIN_RATIONALE_LENGTH:
            action, reason = (
                "reject",
                f"Rationale too short ({len(rationale_stripped)} chars). "
                f"Minimum {MIN_RATIONALE_LENGTH}.",
            )
        else:
            action, reason = "pass", None
    else:
        parsed = parse_all_decisions(store)
        action, reason = _screen_structural(store, proposal, parsed, affected_decision_id)

    if action == "reject":
        return ProposeDecisionResult(
            status="rejected",
            tier=1,
            operation="reject",
            assessment=reason or "Structural validation failed.",
        )

    # --- Tier 2: BM25 similarity (advisory only — does not gate the write) ---
    if parsed is None:
        parsed = parse_all_decisions(store)
    _t2_action, similar_raw = check_bm25_similarity(proposal, parsed)
    similar_models = _to_related_decisions(similar_raw, parsed)

    # --- Commit ---
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

    if similar_models:
        assessment = (
            "Tier 2 surfaced similar decisions; review them and confirm with the "
            "user before proposing further related writes."
        )
    else:
        assessment = "No similar existing decisions found."

    return ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation=actual_operation,
        assessment=assessment,
        similar_decisions=similar_models,
        decision_id=decision_id,
        touched_decisions=list(touched),
        resolved_questions=list(resolve_outcome.resolved_ids),
        relocated_ids=resolve_outcome.relocated_ids or None,
        skipped_prose_ids=resolve_outcome.skipped_prose_ids or None,
    )


def _write_decision_direct(store: Store, proposal: dict) -> str:
    """Write a proposal as a new decision and return the resulting decision id.

    Private helper shared by the validated ``propose_decision`` write path
    and CLI write paths (``nauro note``) that bypass the validation
    pipeline. Updates the in-store hash index after a successful write so
    subsequent Tier 1 checks catch exact duplicates.
    """
    next_num = _next_decision_num(store)
    title = proposal.get("title", "Untitled")
    slug = _slugify(title)
    filename = f"{_decision_number_prefix(next_num)}{slug}"
    rationale = proposal.get("rationale") or title

    decision = Decision(
        date=datetime.now(timezone.utc).date(),
        version=1,
        status=DecisionStatus.active,
        confidence=DecisionConfidence(
            proposal.get("confidence") or DecisionConfidence.medium.value
        ),
        decision_type=_optional_enum(proposal.get("decision_type"), DecisionType),
        reversibility=_optional_enum(proposal.get("reversibility"), Reversibility),
        source=_optional_enum(proposal.get("source"), DecisionSource),
        files_affected=_coerce_files_affected(proposal.get("files_affected")),
        rejected=_coerce_rejected(proposal.get("rejected")),
        num=next_num,
        title=title,
        rationale=rationale,
    )
    store.write_file(_decision_path(filename), format_decision(decision))

    _update_hash_index(store, title, rationale, filename)
    return filename


# ── Tier 1 helpers ────────────────────────────────────────────────────────


def _screen_structural(
    store: Store,
    proposal: dict,
    parsed: list[Decision],
    affected_decision_id: str | None,
) -> tuple[str, str | None]:
    """Run Tier 1 structural screening with hashes + active-title dedup.

    Title dedup keys on active status, not recency: an add or supersede whose
    normalized title matches any active decision is rejected regardless of the
    matched decision's age. ``parsed`` is the shared corpus scan reused for
    Tier 2 BM25, so this does not re-read the store.

    The supersede target is excluded from the dedup set. Screening runs before
    the supersede flip, so the target is still active at this point; without
    the exclusion a same-title supersede of an established decision would
    self-reject. A supersede whose new title collides with a *different* active
    decision still rejects.
    """
    hash_index = _load_hash_index(store)
    existing_hashes = set(hash_index.keys())
    affected_num = extract_decision_number(affected_decision_id) if affected_decision_id else None
    dedup_decisions = [
        d for d in parsed if d.status is DecisionStatus.active and d.num != affected_num
    ]
    return screen_structural(proposal, existing_hashes, dedup_decisions)


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


# ── Tier 2 result reshape ─────────────────────────────────────────────────


def _to_related_decisions(
    raw_hits: list[Bm25Hit],
    parsed_decisions: list[Decision],
) -> list[RelatedDecision]:
    """Lift the ``bm25_retrieve`` dict shape into :class:`RelatedDecision`.

    Matches the unified shape ``check_decision`` already returns; the
    BM25 row dict is normalized into :class:`RelatedDecision` at the
    kernel boundary so every transport renders the same hit.
    """
    by_num: dict[int, Decision] = {d.num: d for d in parsed_decisions}
    out: list[RelatedDecision] = []
    for hit in raw_hits:
        num = hit["number"]
        decision = by_num.get(num)
        status = decision.status.value if decision else DecisionStatus.active.value
        date = decision.date.isoformat() if decision and decision.date else ""
        out.append(
            RelatedDecision(
                id=_canonical_decision_id(num),
                title=hit.get("title", ""),
                score=hit.get("similarity", 0.0),
                status=status,
                date=date,
                rationale_preview=hit.get("rationale_preview", ""),
            )
        )
    return out


# ── Operation execution ───────────────────────────────────────────────────


def _execute_operation(
    store: Store,
    operation: str,
    proposal: dict,
    affected_decision_id: str | None,
) -> tuple[str | None, str, tuple[str, ...], _QuestionResolveOutcome, ErrorPayload | None]:
    """Execute the validated operation against the store.

    Returns:
        ``(decision_id, actual_operation, touched, resolve_outcome, error)``.
        ``touched`` enumerates the decision file stems the kernel rewrote —
        used by the adapter to drive AGENTS.md regen. ``resolve_outcome``
        carries the resolved / relocated / prose-skipped question ids from the
        ``resolves_questions`` ingestion (empty on the error branches, which
        never relocate). On the supersede half-state path ``decision_id`` is
        the newly-written decision and ``error`` names the un-flipped old id.
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
    new_decision_rewritten = new_decision.model_copy(update={"supersedes": str(old_num)})
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
        old_rewritten = old_decision.model_copy(
            update={
                "status": DecisionStatus.superseded,
                "superseded_by": str(new_num),
            }
        )
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
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    additional = (proposal.get("rationale") or "").strip()
    appended_rationale = (
        f"{target.rationale.strip()}\n\n"
        f"*Update (v{target.version + 1}) — {date_str}:* {additional}"
    )
    updated = target.model_copy(
        update={
            "version": target.version + 1,
            "rationale": appended_rationale,
        }
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
    """Stamp named open questions resolved, then self-heal the file layout.

    ``resolve`` flips ``resolved_by`` in place; the following ``normalize``
    relocates every prose-safe stamped entry below ``## Resolved`` (whole-file
    scope, so pre-existing strays heal on the same write) and reports what a
    detached body paragraph held back. The boundary already rejected unknown /
    ambiguous ids, so a failure here can only come from a read/write fault. The
    decision write stands in either case; the error payload names the
    half-state.
    """
    ids = list(proposal.get("resolves_questions") or [])
    if not ids:
        return _QuestionResolveOutcome(), None
    num = extract_decision_number(decision_id)
    if num is None:
        return _QuestionResolveOutcome(), None
    try:
        content = store.read_file(OPEN_QUESTIONS_MD) or ""
        questions_file = OpenQuestionsFile.parse(content)
        result = questions_file.resolve(ids, num, datetime.now(timezone.utc).date())
        normalized = result.file.normalize()
        store.write_file(OPEN_QUESTIONS_MD, normalized.file.format())
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
            resolved_ids=result.moved_ids,
            relocated_ids=normalized.relocated_ids,
            skipped_prose_ids=normalized.skipped_prose_ids,
        ),
        None,
    )


# ── Question-id boundary helpers ──────────────────────────────────────────


def _load_questions_file(store: Store) -> OpenQuestionsFile | None:
    content = store.read_file(OPEN_QUESTIONS_MD)
    if content is None:
        return None
    return OpenQuestionsFile.parse(content)


def _unknown_question_ids(
    ids: list[str],
    questions_file: OpenQuestionsFile | None,
) -> list[str]:
    if questions_file is None:
        return list(ids)
    known = questions_file.known_question_ids
    return [tid for tid in ids if tid not in known]


def _ambiguous_question_ids(
    ids: list[str],
    questions_file: OpenQuestionsFile | None,
) -> dict[str, list[str]]:
    if questions_file is None:
        return {}
    collisions = questions_file.ambiguous_ids
    requested_ambiguous = [tid for tid in ids if tid in collisions]
    if not requested_ambiguous:
        return {}

    counterparts: dict[str, list[str]] = {tid: [] for tid in requested_ambiguous}
    for block in questions_file.blocks:
        if not isinstance(block, EntryBlock):
            continue
        eid = block.entry.id
        if eid in counterparts:
            slot = f"Q{block.entry.num}" if block.entry.num is not None else "<no-Q-id>"
            counterparts[eid].append(slot)
    return counterparts


# ── Decision write plumbing ───────────────────────────────────────────────


def _next_decision_num(store: Store) -> int:
    """Return ``max(existing num) + 1`` over decisions in the store."""
    nums: list[int] = []
    for stem in store.list_decisions():
        n = extract_decision_number(stem)
        if n is not None:
            nums.append(n)
    return max(nums, default=0) + 1


def _slugify(title: str) -> str:
    out_chars: list[str] = []
    prev_dash = False
    for ch in title.lower():
        if ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            out_chars.append("-")
            prev_dash = True
    slug = "".join(out_chars).strip("-")
    if len(slug) > _SLUG_MAX_LENGTH:
        truncated = slug[:_SLUG_MAX_LENGTH]
        # Prefer cutting at a word boundary, but only while that keeps at
        # least half the cap: a title whose only dash sits early would
        # otherwise collapse to a near-empty slug (and filename).
        trimmed = truncated.rsplit("-", 1)[0]
        slug = trimmed if len(trimmed) >= _SLUG_MAX_LENGTH // 2 else truncated
    return slug


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
