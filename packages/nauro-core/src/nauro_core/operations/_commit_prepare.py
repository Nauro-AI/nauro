"""Preparation implementation for hosted judgment commit planning."""

from __future__ import annotations

from . import commit_plan as contract


def _reject_duplicate_keys_impl(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise contract.CanonicalPayloadRejected(
                f"approved payload contains duplicate key {key!r}."
            )
        result[key] = value
    return result


def _parse_payload_impl(payload_bytes: bytes) -> contract.ApprovedPayload:
    try:
        text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise contract.CanonicalPayloadRejected("approved payload must be valid UTF-8.") from exc
    try:
        raw = contract.json.loads(
            text,
            object_pairs_hook=contract._reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                contract.CanonicalPayloadRejected(
                    f"approved payload contains invalid number {value}."
                )
            ),
        )
    except contract.CanonicalPayloadRejected:
        raise
    except (contract.json.JSONDecodeError, TypeError) as exc:
        raise contract.CanonicalPayloadRejected("approved payload must be valid JSON.") from exc
    try:
        payload = contract._PAYLOAD_ADAPTER.validate_python(raw)
    except contract.ValidationError as exc:
        raise contract.CanonicalPayloadRejected(f"approved payload is invalid: {exc}") from exc
    try:
        canonical = contract.canonical_judgment_payload_bytes(payload.model_dump(mode="json"))
    except UnicodeEncodeError as exc:
        raise contract.CanonicalPayloadRejected(
            "approved payload cannot be serialized as canonical UTF-8 JSON."
        ) from exc
    if canonical != payload_bytes:
        raise contract.CanonicalPayloadRejected("approved payload bytes are not canonical JSON.")
    return payload


def _parse_committed_generation_impl(
    generation: contract.CommittedGeneration,
) -> tuple[dict[str, bytes], list[contract.Decision], dict[int, str]]:
    artifact_bytes: dict[str, bytes] = {}
    decisions: list[contract.Decision] = []
    stems_by_number: dict[int, str] = {}
    for artifact in sorted(generation.artifacts, key=lambda value: value.path):
        contract.validate_protected_generation_path(artifact.path)
        try:
            text = artifact.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise contract.CommittedGenerationCorrupt(
                f"committed artifact {artifact.path!r} is not valid UTF-8."
            ) from exc
        artifact_bytes[artifact.path] = bytes(artifact.content)
        if not artifact.path.startswith("decisions/"):
            continue
        filename = artifact.path[len("decisions/") :]
        try:
            decision = contract.parse_decision(text, filename)
        except Exception as exc:
            raise contract.CommittedGenerationCorrupt(
                f"committed decision {artifact.path!r} does not parse."
            ) from exc
        if decision.num <= 0:
            raise contract.CommittedGenerationCorrupt(
                f"committed decision {artifact.path!r} has no positive number."
            )
        if decision.num in stems_by_number:
            raise contract.CommittedGenerationCorrupt(
                f"committed generation contains duplicate decision number {decision.num}."
            )
        decisions.append(decision)
        stems_by_number[decision.num] = filename.removesuffix(".md")
    maximum = max(stems_by_number, default=0)
    if maximum > generation.decision_counter:
        raise contract.CommittedGenerationCorrupt(
            f"published decision number {maximum} exceeds counter {generation.decision_counter}."
        )
    return artifact_bytes, decisions, stems_by_number


def _rejected_result_impl(
    tier: int,
    operation: str,
    assessment: str,
) -> contract.ProposalRejected:
    result_operation = "reject" if tier == 1 else operation
    return contract.ProposalRejected(
        contract.ProposeDecisionResult(
            status="rejected",
            tier=tier,
            operation=result_operation,
            assessment=assessment,
        )
    )


def _target_impl(
    content: contract.JudgmentContent,
    decisions: list[contract.Decision],
    stems_by_number: dict[int, str],
) -> tuple[contract.Decision | None, str | None]:
    if content.operation == "add":
        return None, None
    target_stem = content.affected_decision_id
    assert target_stem is not None
    target = next(
        (decision for decision in decisions if stems_by_number[decision.num] == target_stem),
        None,
    )
    if target is None:
        raise contract._rejected_result(
            1,
            content.operation,
            f"{content.operation} target {target_stem!r} not found in committed generation.",
        )
    return target, target_stem


def _provenance_impl(
    payload: contract.ApprovedPayload,
    attestation: contract.ApprovalAttestation,
) -> tuple[str, contract.DecisionProvenance | None]:
    if isinstance(payload, contract.HostedPreTeamApprovalPayloadV1):
        if not isinstance(attestation, contract.PreTeamApprovalAttestation):
            raise contract.ApprovalAttestationRejected(
                "pre-team payload requires pre-team approval attestation."
            )
        return attestation.request_received_at, None
    if not isinstance(attestation, contract.TeamRatificationAttestation):
        raise contract.ApprovalAttestationRejected(
            "team payload requires team ratification attestation."
        )
    if payload.contributor_operation_id == attestation.ratification_action_id:
        raise contract.ApprovalAttestationRejected(
            "team contributor_operation_id and ratification_action_id must be distinct."
        )
    return (
        attestation.approved_at,
        contract.DecisionProvenance(
            proposed_by=payload.proposed_by,
            approved_by=attestation.approved_by,
            approved_at=attestation.approved_at,
            proposal_id=payload.proposal_id,
            proposed_base_commit=payload.proposed_base_commit,
        ),
    )


def _derive_snapshot_bytes_impl(
    artifacts: tuple[contract.PlannedArtifact, ...],
    *,
    payload: contract.ApprovedPayload,
    effective_at: str,
) -> bytes:
    snapshot_files = {
        artifact.path: artifact.content.decode("utf-8")
        for artifact in artifacts
        if contract.is_hosted_snapshot_content_member(artifact.path)
    }
    title = payload.content.title or ""
    trigger = {
        "add": f"decision: {title}",
        "update": "update: ",
        "supersede": f"supersede: {title}",
    }[payload.content.operation]
    snapshot = contract.serialize_snapshot(
        timestamp=effective_at,
        trigger=trigger,
        files=dict(sorted(snapshot_files.items())),
    )
    return contract.json.dumps(
        snapshot,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _build_artifacts_impl(
    *,
    payload: contract.ApprovedPayload,
    provenance: contract.DecisionProvenance | None,
    effective_at: str,
    generation: contract.CommittedGeneration,
    current: dict[str, bytes],
    evaluation: contract.ProposalEvaluation,
    target: contract.Decision | None,
    target_stem: str | None,
) -> tuple[
    tuple[contract.PlannedArtifact, ...],
    contract.PlannedSnapshot,
    contract.PrimaryDecision,
    contract.ProposeDecisionResult,
    int | None,
    int,
    contract.Decision,
]:
    content = payload.content
    decision_date = contract.date.fromisoformat(effective_at[:10])
    assigned = (
        generation.decision_counter + 1 if content.operation in ("add", "supersede") else None
    )
    new_counter = generation.decision_counter + (1 if assigned is not None else 0)
    planned = dict(current)

    rejected = tuple(
        contract.RejectedAlternative(name=item.alternative, reason=item.reason)
        for item in content.rejected
    )
    if content.operation == "update":
        assert target is not None and target_stem is not None
        primary_model = contract.append_decision_update(
            target,
            additional_rationale=content.rationale,
            update_date=decision_date,
            provenance=provenance,
        )
        primary_stem = target_stem
        primary_path = f"decisions/{primary_stem}.md"
        planned[primary_path] = contract.format_decision(primary_model).encode("utf-8")
        touched = [primary_stem]
    else:
        assert (
            assigned is not None and content.title is not None and content.confidence is not None
        )
        primary_model = contract.build_new_decision(
            number=assigned,
            decision_date=decision_date,
            title=content.title,
            rationale=content.rationale,
            confidence=contract.DecisionConfidence(content.confidence),
            decision_type=contract.DecisionType(content.decision_type)
            if content.decision_type
            else None,
            reversibility=contract.Reversibility(content.reversibility)
            if content.reversibility
            else None,
            source=contract.DecisionSource.mcp,
            files_affected=content.files_affected,
            rejected=rejected,
            provenance=provenance,
        )
        if content.operation == "supersede":
            assert target is not None and target_stem is not None
            primary_model = contract.attach_supersedes(primary_model, target.num)
        primary_stem = (
            f"{contract._decision_number_prefix(assigned)}"
            f"{contract.slugify_decision_title(content.title)}"
        )
        primary_path = f"decisions/{primary_stem}.md"
        planned[primary_path] = contract.format_decision(primary_model).encode("utf-8")
        touched = [primary_stem]
        if content.operation == "supersede":
            assert target is not None and target_stem is not None
            old_path = f"decisions/{target_stem}.md"
            planned[old_path] = contract.format_decision(
                contract.mark_superseded(target, assigned)
            ).encode("utf-8")
            touched.append(target_stem)

    questions_path = "open-questions.md"
    questions_body = planned.get(questions_path, b"").decode("utf-8")
    resolution = contract.resolve_questions_content(
        questions_body,
        question_ids=content.resolves_questions,
        decision_number=primary_model.num,
        resolved_date=decision_date,
    )
    if content.resolves_questions:
        planned[questions_path] = resolution.content.encode("utf-8")

    result = contract.ProposeDecisionResult(
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
        contract.PlannedArtifact(path=path, content=body) for path, body in sorted(planned.items())
    )
    snapshot = contract.PlannedSnapshot(
        content=contract._derive_snapshot_bytes(
            artifacts, payload=payload, effective_at=effective_at
        )
    )
    primary_bytes = planned[primary_path]
    primary = contract.PrimaryDecision(
        decision_id=primary_stem,
        path=primary_path,
        sha256=contract._sha256(primary_bytes),
    )
    return artifacts, snapshot, primary, result, assigned, new_counter, primary_model


def _claim_contract_impl(
    operation: str,
    target: contract.Decision | None,
    primary: contract.Decision,
) -> tuple[tuple[contract.ClaimProbe, ...], contract.ClaimIntents]:
    new_number = primary.num
    new_title = contract.normalize_title(primary.title)
    content_hash = contract.compute_hash(primary.title, primary.rationale)
    content_probe = contract.AbsentContentClaimProbe(content_hash=content_hash)
    acquire_content = contract.ClaimIntent(
        kind="acquire_new_content_hash", content_hash=content_hash
    )
    commit_content = contract.ClaimIntent(kind="commit_content_hash", content_hash=content_hash)
    if operation == "add":
        probes: tuple[contract.ClaimProbe, ...] = (
            contract.AbsentTitleClaimProbe(normalized_title=new_title),
            content_probe,
        )
        intents = contract.ClaimIntents(
            entry=(
                contract.ClaimIntent(
                    kind="acquire_new_title",
                    normalized_title=new_title,
                    owner_decision_number=new_number,
                ),
                acquire_content,
            ),
            publication=(
                contract.ClaimIntent(
                    kind="commit_title_owner",
                    normalized_title=new_title,
                    owner_decision_number=new_number,
                ),
                commit_content,
            ),
        )
        return probes, intents
    assert target is not None
    old_title = contract.normalize_title(target.title)
    if operation == "update":
        probes = (
            contract.OwnedTitleClaimProbe(
                normalized_title=old_title,
                expected_owner_decision_number=target.num,
            ),
            content_probe,
        )
        intents = contract.ClaimIntents(
            entry=(
                contract.ClaimIntent(
                    kind="hold_owned_title",
                    normalized_title=old_title,
                    owner_decision_number=target.num,
                ),
                acquire_content,
            ),
            publication=(
                contract.ClaimIntent(
                    kind="retain_title_owner",
                    normalized_title=old_title,
                    owner_decision_number=target.num,
                ),
                commit_content,
            ),
        )
        return probes, intents
    if new_title == old_title:
        probes = (
            contract.OwnedTitleClaimProbe(
                normalized_title=old_title,
                expected_owner_decision_number=target.num,
            ),
            content_probe,
        )
        intents = contract.ClaimIntents(
            entry=(
                contract.ClaimIntent(
                    kind="acquire_title_transfer",
                    normalized_title=old_title,
                    from_owner_decision_number=target.num,
                    to_owner_decision_number=new_number,
                ),
                acquire_content,
            ),
            publication=(
                contract.ClaimIntent(
                    kind="transfer_title_owner",
                    normalized_title=old_title,
                    from_owner_decision_number=target.num,
                    to_owner_decision_number=new_number,
                ),
                commit_content,
            ),
        )
        return probes, intents
    probes = (
        contract.OwnedTitleClaimProbe(
            normalized_title=old_title,
            expected_owner_decision_number=target.num,
        ),
        contract.AbsentTitleClaimProbe(normalized_title=new_title),
        content_probe,
    )
    intents = contract.ClaimIntents(
        entry=(
            contract.ClaimIntent(
                kind="hold_owned_title",
                normalized_title=old_title,
                owner_decision_number=target.num,
            ),
            contract.ClaimIntent(
                kind="acquire_new_title",
                normalized_title=new_title,
                owner_decision_number=new_number,
            ),
            acquire_content,
        ),
        publication=(
            contract.ClaimIntent(
                kind="release_title_owner",
                normalized_title=old_title,
                owner_decision_number=target.num,
            ),
            contract.ClaimIntent(
                kind="commit_title_owner",
                normalized_title=new_title,
                owner_decision_number=new_number,
            ),
            commit_content,
        ),
    )
    return probes, intents


def prepare_judgment_commit_impl(
    payload_bytes: bytes,
    approval_attestation: contract.ApprovalAttestation,
    committed_generation: contract.CommittedGeneration,
    expected_payload_digest: str | None = None,
) -> contract.PreparedJudgmentCommit:
    """Prepare immutable artifacts and semantic claim reads without I/O."""
    payload_bytes = bytes(payload_bytes)
    digest = contract._sha256(payload_bytes)
    if expected_payload_digest is not None:
        contract._validate_sha256(expected_payload_digest, field="expected_payload_digest")
        if digest != expected_payload_digest:
            raise contract.ApprovedPayloadDigestMismatch(
                "expected payload digest does not match approved payload bytes."
            )
    payload = contract._parse_payload(payload_bytes)
    if (
        payload.base_generation_id != committed_generation.generation_id
        or payload.base_decision_counter != committed_generation.decision_counter
    ):
        raise contract.ApprovedBaseStale(
            "approved base generation and counter do not match the observed committed generation."
        )
    current, decisions, stems_by_number = contract._parse_committed_generation(
        committed_generation
    )
    target, target_stem = contract._target(payload.content, decisions, stems_by_number)
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
        "source": contract.DecisionSource.mcp.value,
        "base_commit": None,
    }
    questions_file = (
        contract.OpenQuestionsFile.parse(questions_bytes.decode("utf-8"))
        if questions_bytes is not None and payload.content.resolves_questions
        else None
    )
    request_rejection = contract.validate_proposal_request(
        proposal,
        operation=payload.content.operation,
        questions_file=questions_file,
    )
    if request_rejection is not None:
        raise contract.ProposalRejected(request_rejection)
    evaluation = contract.evaluate_parsed_proposal(
        proposal,
        operation=payload.content.operation,
        decisions=decisions,
        existing_hashes=set(),
        affected_number=target.num if target is not None else None,
        enforce_claim_conflicts=False,
    )
    if isinstance(evaluation, contract.ProposeDecisionResult):
        raise contract.ProposalRejected(evaluation)
    effective_at, provenance = contract._provenance(payload, approval_attestation)
    artifacts, snapshot, primary, result, assigned, new_counter, primary_model = (
        contract._build_artifacts(
            payload=payload,
            provenance=provenance,
            effective_at=effective_at,
            generation=committed_generation,
            current=current,
            evaluation=evaluation,
            target=target,
            target_stem=target_stem,
        )
    )
    probes, intents = contract._claim_contract(payload.content.operation, target, primary_model)
    return contract.PreparedJudgmentCommit(
        payload_bytes=payload_bytes,
        payload_digest=digest,
        payload=payload,
        approval_attestation=approval_attestation,
        base=contract.PreparedBase(
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
        primary_decision=primary,
        claim_probes=probes,
        claim_intents=intents,
        result_projection=contract._CommittedResultProjection.from_result(result),
    )
