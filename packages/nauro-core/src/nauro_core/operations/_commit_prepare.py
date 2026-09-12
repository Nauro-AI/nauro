"""Preparation implementation for hosted judgment commit planning."""

from __future__ import annotations

from datetime import date
from typing import Literal

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
    parse_decision,
)
from nauro_core.operations._commit_contract import (
    ApprovalAttestation,
    ApprovedBaseStale,
    ApprovedPayload,
    ApprovedPayloadDigestMismatch,
    CommittedGeneration,
    CommittedGenerationCorrupt,
    JudgmentContent,
    PlannedArtifact,
    PlannedSnapshot,
    PreparedBase,
    PreparedJudgmentCommit,
    PrimaryDecision,
    ProposalRejected,
    _CommittedResultProjection,
    build_claim_contract,
    derive_provenance,
    derive_snapshot_bytes,
    parse_payload,
    sha256_hex,
    validate_sha256,
)
from nauro_core.operations._decision_transitions import (
    DecisionProvenance,
    append_decision_update,
    attach_supersedes,
    build_new_decision,
    mark_superseded,
    resolve_questions_content,
    slugify_decision_title,
)
from nauro_core.operations._proposal_evaluation import (
    ProposalEvaluation,
    evaluate_parsed_proposal,
    validate_proposal_request,
)
from nauro_core.operations.results import ProposeDecisionResult
from nauro_core.parsing import _decision_number_prefix
from nauro_core.protected_generation_membership import (
    validate_protected_generation_path,
)
from nauro_core.questions import OpenQuestionsFile


def _parse_committed_generation(
    generation: CommittedGeneration,
) -> tuple[dict[str, bytes], list[Decision], dict[int, str]]:
    artifact_bytes: dict[str, bytes] = {}
    decisions: list[Decision] = []
    stems_by_number: dict[int, str] = {}
    for artifact in sorted(generation.artifacts, key=lambda value: value.path):
        validate_protected_generation_path(artifact.path)
        try:
            text = artifact.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CommittedGenerationCorrupt(
                f"committed artifact {artifact.path!r} is not valid UTF-8."
            ) from exc
        artifact_bytes[artifact.path] = bytes(artifact.content)
        if not artifact.path.startswith("decisions/"):
            continue
        filename = artifact.path[len("decisions/") :]
        try:
            decision = parse_decision(text, filename)
        except Exception as exc:
            raise CommittedGenerationCorrupt(
                f"committed decision {artifact.path!r} does not parse."
            ) from exc
        if decision.num <= 0:
            raise CommittedGenerationCorrupt(
                f"committed decision {artifact.path!r} has no positive number."
            )
        if decision.num in stems_by_number:
            raise CommittedGenerationCorrupt(
                f"committed generation contains duplicate decision number {decision.num}."
            )
        decisions.append(decision)
        stems_by_number[decision.num] = filename.removesuffix(".md")
    maximum = max(stems_by_number, default=0)
    if maximum > generation.decision_counter:
        raise CommittedGenerationCorrupt(
            f"published decision number {maximum} exceeds counter {generation.decision_counter}."
        )
    return artifact_bytes, decisions, stems_by_number


def _rejected_result(
    tier: int,
    operation: Literal["add", "update", "supersede"],
    assessment: str,
) -> ProposalRejected:
    result_operation = "reject" if tier == 1 else operation
    return ProposalRejected(
        ProposeDecisionResult(
            status="rejected",
            tier=tier,
            operation=result_operation,
            assessment=assessment,
        )
    )


def _target(
    content: JudgmentContent,
    decisions: list[Decision],
    stems_by_number: dict[int, str],
) -> tuple[Decision | None, str | None]:
    if content.operation == "add":
        return None, None
    target_stem = content.affected_decision_id
    assert target_stem is not None
    target = next(
        (decision for decision in decisions if stems_by_number[decision.num] == target_stem),
        None,
    )
    if target is None:
        raise _rejected_result(
            1,
            content.operation,
            f"{content.operation} target {target_stem!r} not found in committed generation.",
        )
    return target, target_stem


def _build_artifacts(
    *,
    payload: ApprovedPayload,
    provenance: DecisionProvenance | None,
    effective_at: str,
    generation: CommittedGeneration,
    current: dict[str, bytes],
    evaluation: ProposalEvaluation,
    target: Decision | None,
    target_stem: str | None,
    snapshot_format: Literal["serialized", "references"] = "serialized",
) -> tuple[
    tuple[PlannedArtifact, ...],
    PlannedSnapshot,
    PrimaryDecision,
    ProposeDecisionResult,
    int | None,
    int,
    Decision,
]:
    content = payload.content
    decision_date = date.fromisoformat(effective_at[:10])
    assigned = (
        generation.decision_counter + 1 if content.operation in ("add", "supersede") else None
    )
    new_counter = generation.decision_counter + (1 if assigned is not None else 0)
    planned = dict(current)

    rejected = tuple(
        RejectedAlternative(name=item.alternative, reason=item.reason) for item in content.rejected
    )
    if content.operation == "update":
        assert target is not None and target_stem is not None
        primary_model = append_decision_update(
            target,
            additional_rationale=content.rationale,
            update_date=decision_date,
            provenance=provenance,
        )
        primary_stem = target_stem
        primary_path = f"decisions/{primary_stem}.md"
        planned[primary_path] = format_decision(primary_model).encode("utf-8")
        touched = [primary_stem]
    else:
        assert assigned is not None and content.title is not None and content.confidence is not None
        primary_model = build_new_decision(
            number=assigned,
            decision_date=decision_date,
            title=content.title,
            rationale=content.rationale,
            confidence=DecisionConfidence(content.confidence),
            decision_type=DecisionType(content.decision_type) if content.decision_type else None,
            reversibility=Reversibility(content.reversibility) if content.reversibility else None,
            source=DecisionSource.mcp,
            files_affected=content.files_affected,
            rejected=rejected,
            provenance=provenance,
        )
        if content.operation == "supersede":
            assert target is not None and target_stem is not None
            primary_model = attach_supersedes(primary_model, target.num)
        primary_stem = f"{_decision_number_prefix(assigned)}{slugify_decision_title(content.title)}"
        primary_path = f"decisions/{primary_stem}.md"
        planned[primary_path] = format_decision(primary_model).encode("utf-8")
        touched = [primary_stem]
        if content.operation == "supersede":
            assert target is not None and target_stem is not None
            old_path = f"decisions/{target_stem}.md"
            planned[old_path] = format_decision(mark_superseded(target, assigned)).encode("utf-8")
            touched.append(target_stem)

    questions_path = "open-questions.md"
    questions_body = planned.get(questions_path, b"").decode("utf-8")
    resolution = resolve_questions_content(
        questions_body,
        question_ids=content.resolves_questions,
        decision_number=primary_model.num,
        resolved_date=decision_date,
    )
    if content.resolves_questions:
        planned[questions_path] = resolution.content.encode("utf-8")

    result = ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation=content.operation,
        assessment=evaluation.assessment,
        similar_decisions=list(evaluation.similar_decisions),
        decision_id=primary_stem,
        touched_decisions=touched,
        resolved_questions=list(resolution.resolved_ids),
        relocated_ids=resolution.relocated_ids or None,
        skipped_prose_ids=resolution.skipped_prose_ids or None,
    )
    artifacts = tuple(
        PlannedArtifact(path=path, content=body) for path, body in sorted(planned.items())
    )
    snapshot = PlannedSnapshot(
        content=derive_snapshot_bytes(
            artifacts, payload=payload, effective_at=effective_at, snapshot_format=snapshot_format
        )
    )
    primary_bytes = planned[primary_path]
    primary = PrimaryDecision(
        decision_id=primary_stem,
        path=primary_path,
        sha256=sha256_hex(primary_bytes),
    )
    return artifacts, snapshot, primary, result, assigned, new_counter, primary_model


def prepare_judgment_commit(
    payload_bytes: bytes,
    approval_attestation: ApprovalAttestation,
    committed_generation: CommittedGeneration,
    expected_payload_digest: str | None = None,
    *,
    snapshot_format: Literal["serialized", "references"] = "serialized",
) -> PreparedJudgmentCommit:
    """Prepare immutable artifacts and semantic claim reads without I/O."""
    payload_bytes = bytes(payload_bytes)
    digest = sha256_hex(payload_bytes)
    if expected_payload_digest is not None:
        validate_sha256(expected_payload_digest, field="expected_payload_digest")
        if digest != expected_payload_digest:
            raise ApprovedPayloadDigestMismatch(
                "expected payload digest does not match approved payload bytes."
            )
    payload = parse_payload(payload_bytes)
    if (
        payload.base_generation_id != committed_generation.generation_id
        or payload.base_decision_counter != committed_generation.decision_counter
    ):
        raise ApprovedBaseStale(
            "approved base generation and counter do not match the observed committed generation."
        )
    current, decisions, stems_by_number = _parse_committed_generation(committed_generation)
    target, target_stem = _target(payload.content, decisions, stems_by_number)
    questions_bytes = current.get("open-questions.md")
    proposal = {
        "title": payload.content.title,
        "rationale": payload.content.rationale,
        "rejected": [
            {"alternative": value.alternative, "reason": value.reason}
            for value in payload.content.rejected
        ],
        "confidence": payload.content.confidence,
        "decision_type": payload.content.decision_type,
        "reversibility": payload.content.reversibility,
        "files_affected": list(payload.content.files_affected),
        "resolves_questions": list(payload.content.resolves_questions),
        "source": DecisionSource.mcp.value,
        "base_commit": None,
    }
    questions_file = (
        OpenQuestionsFile.parse(questions_bytes.decode("utf-8"))
        if questions_bytes is not None and payload.content.resolves_questions
        else None
    )
    request_rejection = validate_proposal_request(
        proposal,
        operation=payload.content.operation,
        questions_file=questions_file,
    )
    if request_rejection is not None:
        raise ProposalRejected(request_rejection)
    evaluation = evaluate_parsed_proposal(
        proposal,
        operation=payload.content.operation,
        decisions=decisions,
        existing_hashes=set(),
        affected_number=target.num if target is not None else None,
        enforce_claim_conflicts=False,
    )
    if isinstance(evaluation, ProposeDecisionResult):
        raise ProposalRejected(evaluation)
    effective_at, provenance = derive_provenance(payload, approval_attestation)
    artifacts, snapshot, primary, result, assigned, new_counter, primary_model = _build_artifacts(
        payload=payload,
        provenance=provenance,
        effective_at=effective_at,
        generation=committed_generation,
        current=current,
        evaluation=evaluation,
        target=target,
        target_stem=target_stem,
        snapshot_format=snapshot_format,
    )
    probes, intents = build_claim_contract(payload.content.operation, target, primary_model)
    return PreparedJudgmentCommit(
        payload_bytes=payload_bytes,
        payload_digest=digest,
        payload=payload,
        approval_attestation=approval_attestation,
        base=PreparedBase(
            generation_id=committed_generation.generation_id,
            decision_counter=committed_generation.decision_counter,
            observed_manifest_digest=committed_generation.observed_manifest_digest,
        ),
        effective_at=effective_at,
        decision_provenance=provenance,
        assigned_decision_number=assigned,
        new_decision_counter=new_counter,
        planned_artifacts=artifacts,
        snapshot=snapshot,
        snapshot_format=snapshot_format,
        primary_decision=primary,
        claim_probes=probes,
        claim_intents=intents,
        result_projection=_CommittedResultProjection.from_result(result),
    )
