from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from pydantic import ValidationError

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
    parse_decision,
)
from nauro_core.operations.commit_plan import (
    AbsentContentClaimObservation,
    AbsentTitleClaimObservation,
    AbsentTitleClaimProbe,
    ApprovalAttestationRejected,
    ApprovedBaseStale,
    ApprovedPayloadDigestMismatch,
    CanonicalPayloadRejected,
    ClaimUnavailable,
    CommittedArtifact,
    CommittedContentClaimObservation,
    CommittedGeneration,
    CommittedGenerationCorrupt,
    CommittedTitleClaimObservation,
    ContentClaimConflict,
    DuplicateClaimObservation,
    MalformedClaimObservation,
    MismatchedClaimObservation,
    MissingClaimObservation,
    PlannedSnapshot,
    PreparedJudgmentCommit,
    PreTeamApprovalAttestation,
    ProposalRejected,
    TeamRatificationAttestation,
    TitleClaimConflict,
    UnavailableContentClaimObservation,
    UnexpectedClaimObservation,
    canonical_judgment_payload_bytes,
    finalize_judgment_commit,
    prepare_judgment_commit,
)
from nauro_core.operations.results import ProposeDecisionResult
from nauro_core.snapshot import serialize_snapshot

GENERATION_ID = "01K00000000000000000000000"
PROPOSAL_ID = "01K00000000000000000000001"
AUTHOR_ID = "01K00000000000000000000002"
RATIFIER_ID = "01K00000000000000000000003"
ACTION_ID = "01K00000000000000000000004"
TIMESTAMP = "2026-08-14T09:10:11.123456Z"
_UNSET = object()


def _decision(
    num: int,
    title: str,
    *,
    status: DecisionStatus = DecisionStatus.active,
    superseded_by: str | None = None,
    proposed_by: str | None = None,
    approved_by: str | None = None,
    approved_at: str | None = None,
    proposal_id: str | None = None,
) -> tuple[str, bytes]:
    stem = f"{num:03d}-{title.lower().replace(' ', '-')}"
    model = Decision(
        date=date(2026, 1, 1),
        confidence=DecisionConfidence.medium,
        status=status,
        superseded_by=superseded_by,
        num=num,
        title=title,
        rationale="Existing rationale with enough text for validation.",
        proposed_by=proposed_by,
        approved_by=approved_by,
        approved_at=approved_at,
        proposal_id=proposal_id,
    )
    return f"decisions/{stem}.md", format_decision(model).encode()


def _generation(*entries: tuple[str, bytes], counter: int | None = None) -> CommittedGeneration:
    artifacts = tuple(
        CommittedArtifact(
            path=path,
            content=content,
            manifest_artifact_digest=hashlib.sha256(content).hexdigest(),
        )
        for path, content in entries
    )
    numbers = [
        int(path.removeprefix("decisions/")[:3])
        for path, _ in entries
        if path.startswith("decisions/")
    ]
    return CommittedGeneration(
        generation_id=GENERATION_ID,
        decision_counter=max(numbers, default=0) if counter is None else counter,
        observed_manifest_digest="a" * 64,
        artifacts=artifacts,
    )


def _content(
    *,
    operation: str = "add",
    target: str | None = None,
    title: str | None | object = _UNSET,
    rationale: str = "SQLite provides durable local state with simple operational ownership.",
    resolves_questions: list[str] | None = None,
) -> dict[str, object]:
    effective_title = (
        (None if operation == "update" else "Use SQLite for durable local state")
        if title is _UNSET
        else title
    )
    return {
        "affected_decision_id": target,
        "confidence": None if operation == "update" else "medium",
        "decision_type": None,
        "files_affected": [],
        "operation": operation,
        "rationale": rationale,
        "rejected": [],
        "resolves_questions": resolves_questions or [],
        "reversibility": None,
        "title": effective_title,
    }


def _preteam_payload(generation: CommittedGeneration, **content_overrides: object) -> bytes:
    content = _content(**content_overrides)
    return canonical_judgment_payload_bytes(
        {
            "approval_mode": "pre_team_session",
            "base_decision_counter": generation.decision_counter,
            "base_generation_id": generation.generation_id,
            "content": content,
            "payload_schema": "nauro.judgment_commit.pre_team.v1",
            "proposed_base_commit": None,
        }
    )


def _team_payload(
    generation: CommittedGeneration,
    *,
    contributor_operation_id: str = "client-op-1",
    **content_overrides: object,
) -> bytes:
    return canonical_judgment_payload_bytes(
        {
            "approval_mode": "team_ratification",
            "base_decision_counter": generation.decision_counter,
            "base_generation_id": generation.generation_id,
            "content": _content(**content_overrides),
            "contributor_operation_id": contributor_operation_id,
            "payload_schema": "nauro.judgment_commit.team_proposal.v1",
            "proposal_id": PROPOSAL_ID,
            "proposal_revision": 1,
            "proposed_base_commit": "b" * 40,
            "proposed_by": AUTHOR_ID,
        }
    )


def _preteam_attestation(
    operation_id: str = "client-op-1",
) -> PreTeamApprovalAttestation:
    return PreTeamApprovalAttestation(
        actor_id=AUTHOR_ID,
        operation_id=operation_id,
        request_received_at=TIMESTAMP,
    )


def _team_attestation() -> TeamRatificationAttestation:
    return TeamRatificationAttestation(
        approved_by=RATIFIER_ID,
        approved_at=TIMESTAMP,
        ratification_action_id=ACTION_ID,
    )


def test_prepare_and_finalize_add_are_deterministic_and_storage_neutral() -> None:
    generation = _generation(("project.md", b"# Test\n"), counter=4)
    payload = _preteam_payload(generation)

    first = prepare_judgment_commit(payload, _preteam_attestation(), generation)
    second = prepare_judgment_commit(payload, _preteam_attestation(), generation)

    assert first == second
    assert first.payload_bytes == payload
    assert first.payload_digest == hashlib.sha256(payload).hexdigest()
    assert first.assigned_decision_number == 5
    assert first.new_decision_counter == 5
    assert [probe.kind for probe in first.claim_probes] == [
        "absent_title",
        "absent_content",
    ]
    observations = [
        AbsentContentClaimObservation(content_hash=first.claim_probes[1].content_hash),
        AbsentTitleClaimObservation(normalized_title=first.claim_probes[0].normalized_title),
    ]
    plan = finalize_judgment_commit(first, observations)
    replay = finalize_judgment_commit(second, list(reversed(observations)))
    assert plan.plan_record_bytes == replay.plan_record_bytes
    assert plan.plan_record_digest == hashlib.sha256(plan.plan_record_bytes).hexdigest()
    assert json.loads(plan.plan_record_bytes)["transform_version"] == first.transformation_version
    expected_result = ProposeDecisionResult(
        status="confirmed",
        tier=2,
        operation="add",
        assessment="No similar existing decisions found.",
        decision_id="005-use-sqlite-for-durable-local-state",
        touched_decisions=["005-use-sqlite-for-durable-local-state"],
    )
    assert first.committed_result == expected_result
    assert plan.committed_result == expected_result


def test_prepare_rejects_noncanonical_payload_and_digest_mismatch() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    canonical = _preteam_payload(generation)
    noncanonical = json.dumps(json.loads(canonical), indent=2).encode()
    with pytest.raises(CanonicalPayloadRejected, match="canonical"):
        prepare_judgment_commit(noncanonical, _preteam_attestation(), generation)
    with pytest.raises(ApprovedPayloadDigestMismatch):
        prepare_judgment_commit(
            canonical,
            _preteam_attestation(),
            generation,
            expected_payload_digest="f" * 64,
        )


def test_prepare_rejects_duplicate_json_keys() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    payload = _preteam_payload(generation)
    duplicate = payload[:-1] + b',"payload_schema":"nauro.judgment_commit.pre_team.v1"}'
    with pytest.raises(CanonicalPayloadRejected, match="duplicate"):
        prepare_judgment_commit(duplicate, _preteam_attestation(), generation)


def test_preteam_payload_rejects_team_ratification_attestation() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    with pytest.raises(
        ApprovalAttestationRejected,
        match="pre-team payload requires pre-team approval attestation",
    ):
        prepare_judgment_commit(_preteam_payload(generation), _team_attestation(), generation)


def test_team_payload_rejects_preteam_approval_attestation() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    with pytest.raises(
        ApprovalAttestationRejected,
        match="team payload requires team ratification attestation",
    ):
        prepare_judgment_commit(_team_payload(generation), _preteam_attestation(), generation)


def test_team_preparation_rejects_reused_submission_and_ratification_ids() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    with pytest.raises(ApprovalAttestationRejected, match="must be distinct"):
        prepare_judgment_commit(
            _team_payload(generation, contributor_operation_id=ACTION_ID),
            _team_attestation(),
            generation,
        )


def test_team_preparation_accepts_distinct_submission_and_ratification_ids() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(_team_payload(generation), _team_attestation(), generation)
    assert prepared.payload.contributor_operation_id == "client-op-1"
    assert prepared.approval_attestation.ratification_action_id == ACTION_ID


def test_preteam_preparation_does_not_apply_team_action_id_distinction() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation),
        _preteam_attestation(operation_id=ACTION_ID),
        generation,
    )
    assert prepared.approval_attestation.operation_id == ACTION_ID


def test_prepare_rejects_escaped_unpaired_surrogate_as_non_utf8_canonical_data() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    raw = json.loads(_preteam_payload(generation))
    raw["content"]["title"] = "Invalid \ud800 title"
    escaped = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(CanonicalPayloadRejected, match="canonical UTF-8"):
        prepare_judgment_commit(escaped, _preteam_attestation(), generation)


def test_prepare_round_trips_canonical_non_ascii_payload() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    payload = _preteam_payload(
        generation,
        title="Use SQLite for durable café state",
    )
    prepared = prepare_judgment_commit(payload, _preteam_attestation(), generation)
    assert prepared.payload_bytes == payload
    assert prepared.payload.content.title == "Use SQLite for durable café state"
    assert b"caf\xc3\xa9" in next(
        artifact.content
        for artifact in prepared.planned_artifacts
        if artifact.path == prepared.primary_decision.path
    )


@pytest.mark.parametrize(
    ("content_overrides", "tier", "operation", "assessment"),
    [
        ({"title": ""}, 1, "reject", "Title is empty."),
        (
            {"rationale": "too short"},
            1,
            "reject",
            "Rationale too short (9 chars). Minimum 20.",
        ),
        (
            {"resolves_questions": ["Q404"]},
            0,
            "add",
            "resolves_questions contains unknown id(s): 'Q404'. Call get_context "
            "(L0 lists every open question) to see the canonical ids in "
            "open-questions.md.",
        ),
    ],
)
def test_semantic_payload_rejections_preserve_existing_result_semantics(
    content_overrides: dict[str, object],
    tier: int,
    operation: str,
    assessment: str,
) -> None:
    generation = _generation(("project.md", b"# Test\n"))
    with pytest.raises(ProposalRejected) as raised:
        prepare_judgment_commit(
            _preteam_payload(generation, **content_overrides),
            _preteam_attestation(),
            generation,
        )
    assert raised.value.result.model_dump(mode="json") == {
        "assessment": assessment,
        "decision_id": None,
        "error": None,
        "operation": operation,
        "relocated_ids": None,
        "resolved_questions": [],
        "similar_decisions": [],
        "skipped_prose_ids": None,
        "status": "rejected",
        "tier": tier,
        "touched_decisions": [],
    }


def test_update_disallowed_fields_are_semantic_tier_zero_rejection() -> None:
    existing = _decision(1, "Existing decision")
    generation = _generation(existing, counter=1)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")
    with pytest.raises(ProposalRejected) as raised:
        prepare_judgment_commit(
            _preteam_payload(
                generation,
                operation="update",
                target=target,
                title="Replacement title",
            ),
            _preteam_attestation(),
            generation,
        )
    assert raised.value.result.model_dump(mode="json") == {
        "assessment": (
            'operation="update" appends rationale only; cannot change title. '
            'Use operation="supersede" to replace the decision with new metadata.'
        ),
        "decision_id": None,
        "error": None,
        "operation": "update",
        "relocated_ids": None,
        "resolved_questions": [],
        "similar_decisions": [],
        "skipped_prose_ids": None,
        "status": "rejected",
        "tier": 0,
        "touched_decisions": [],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_generation_id", PROPOSAL_ID),
        ("base_decision_counter", 1),
    ],
)
def test_prepare_rejects_stale_generation_or_counter_without_plan(
    field: str, value: object
) -> None:
    generation = _generation(("project.md", b"# Test\n"), counter=2)
    payload = json.loads(_preteam_payload(generation))
    payload[field] = value
    with pytest.raises(ApprovedBaseStale):
        prepare_judgment_commit(
            canonical_judgment_payload_bytes(payload),
            _preteam_attestation(),
            generation,
        )


def test_counter_gaps_remain_reserved_and_add_uses_counter_plus_one() -> None:
    generation = _generation(_decision(2, "Existing decision"), counter=9)
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    assert prepared.assigned_decision_number == 10
    assert prepared.new_decision_counter == 10


def test_counter_below_published_number_is_corruption() -> None:
    generation = _generation(_decision(4, "Existing decision"), counter=3)
    with pytest.raises(CommittedGenerationCorrupt, match="counter"):
        prepare_judgment_commit(_preteam_payload(generation), _preteam_attestation(), generation)


def test_team_add_applies_complete_provenance_and_exact_snapshot_bytes() -> None:
    generation = _generation(
        ("project.md", b"# Test\n"),
        ("context/brief-one.md", b"private\n"),
        ("questions-provenance.json", b"{}\n"),
    )
    prepared = prepare_judgment_commit(_team_payload(generation), _team_attestation(), generation)
    body = next(
        artifact.content.decode()
        for artifact in prepared.planned_artifacts
        if artifact.path == prepared.primary_decision.path
    )
    decision = parse_decision(body, prepared.primary_decision.path.removeprefix("decisions/"))
    assert decision.proposed_by == AUTHOR_ID
    assert decision.approved_by == RATIFIER_ID
    assert decision.approved_at == TIMESTAMP
    assert decision.proposal_id == PROPOSAL_ID
    assert decision.proposed_base_commit == "b" * 40
    assert b"context/brief-one.md" not in prepared.snapshot.content
    assert b"questions-provenance.json" not in prepared.snapshot.content
    files = {
        artifact.path: artifact.content.decode()
        for artifact in prepared.planned_artifacts
        if artifact.path in {"project.md", prepared.primary_decision.path}
    }
    expected = serialize_snapshot(
        timestamp=TIMESTAMP,
        trigger="decision: Use SQLite for durable local state",
        files=dict(sorted(files.items())),
    )
    assert (
        prepared.snapshot.content
        == json.dumps(
            expected, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    )


def test_preteam_add_emits_no_team_provenance() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    body = next(
        artifact.content.decode()
        for artifact in prepared.planned_artifacts
        if artifact.path == prepared.primary_decision.path
    )
    assert "proposed_by:" not in body
    assert "approved_by:" not in body
    assert "approved_at:" not in body
    assert "proposal_id:" not in body
    assert "proposed_base_commit:" not in body


def test_team_update_replaces_provenance_and_preteam_update_clears_it() -> None:
    existing = _decision(
        1,
        "Existing decision",
        proposed_by=AUTHOR_ID,
        approved_by=RATIFIER_ID,
        approved_at=TIMESTAMP,
        proposal_id=PROPOSAL_ID,
    )
    generation = _generation(existing, counter=1)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")

    team = prepare_judgment_commit(
        _team_payload(generation, operation="update", target=target),
        _team_attestation(),
        generation,
    )
    team_body = next(a.content.decode() for a in team.planned_artifacts if a.path == existing[0])
    team_decision = parse_decision(team_body, existing[0].removeprefix("decisions/"))
    assert team_decision.proposed_base_commit == "b" * 40

    preteam = prepare_judgment_commit(
        _preteam_payload(generation, operation="update", target=target),
        _preteam_attestation(),
        generation,
    )
    preteam_body = next(
        a.content.decode() for a in preteam.planned_artifacts if a.path == existing[0]
    )
    assert "proposed_by:" not in preteam_body
    assert "approved_by:" not in preteam_body


def test_supersede_preserves_target_provenance_and_same_title_has_one_probe() -> None:
    existing = _decision(
        1,
        "Existing decision",
        proposed_by=AUTHOR_ID,
        approved_by=RATIFIER_ID,
        approved_at=TIMESTAMP,
        proposal_id=PROPOSAL_ID,
    )
    generation = _generation(existing, counter=1)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")
    prepared = prepare_judgment_commit(
        _team_payload(
            generation,
            operation="supersede",
            target=target,
            title="Existing decision",
        ),
        _team_attestation(),
        generation,
    )
    assert [probe.kind for probe in prepared.claim_probes] == [
        "owned_title",
        "absent_content",
    ]
    assert prepared.claim_intents.entry[0].kind == "acquire_title_transfer"
    old_body = next(
        a.content.decode() for a in prepared.planned_artifacts if a.path == existing[0]
    )
    old = parse_decision(old_body, existing[0].removeprefix("decisions/"))
    assert old.proposal_id == PROPOSAL_ID
    assert old.status is DecisionStatus.superseded


def test_different_title_supersede_has_hold_acquire_release_commit_contract() -> None:
    existing = _decision(1, "Existing decision")
    generation = _generation(existing, counter=1)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")
    prepared = prepare_judgment_commit(
        _preteam_payload(
            generation,
            operation="supersede",
            target=target,
            title="Replacement decision",
        ),
        _preteam_attestation(),
        generation,
    )
    assert [probe.kind for probe in prepared.claim_probes] == [
        "owned_title",
        "absent_title",
        "absent_content",
    ]
    assert [intent.kind for intent in prepared.claim_intents.entry] == [
        "hold_owned_title",
        "acquire_new_title",
        "acquire_new_content_hash",
    ]
    assert [intent.kind for intent in prepared.claim_intents.publication] == [
        "release_title_owner",
        "commit_title_owner",
        "commit_content_hash",
    ]


def test_update_claim_contract_keeps_counter_and_title_owner() -> None:
    existing = _decision(3, "Existing decision")
    generation = _generation(existing, counter=8)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")
    prepared = prepare_judgment_commit(
        _preteam_payload(generation, operation="update", target=target),
        _preteam_attestation(),
        generation,
    )
    assert prepared.assigned_decision_number is None
    assert prepared.new_decision_counter == 8
    assert [probe.kind for probe in prepared.claim_probes] == [
        "owned_title",
        "absent_content",
    ]
    assert [intent.kind for intent in prepared.claim_intents.publication] == [
        "retain_title_owner",
        "commit_content_hash",
    ]


def test_finalize_rejects_missing_extra_duplicate_malformed_and_unavailable() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    title = prepared.claim_probes[0].normalized_title
    content_hash = prepared.claim_probes[1].content_hash
    title_observation = AbsentTitleClaimObservation(normalized_title=title)
    content_observation = AbsentContentClaimObservation(content_hash=content_hash)

    with pytest.raises(MissingClaimObservation):
        finalize_judgment_commit(prepared, [title_observation])
    with pytest.raises(UnexpectedClaimObservation):
        finalize_judgment_commit(
            prepared,
            [
                title_observation,
                content_observation,
                AbsentTitleClaimObservation(normalized_title="other"),
            ],
        )
    with pytest.raises(DuplicateClaimObservation):
        finalize_judgment_commit(
            prepared, [title_observation, title_observation, content_observation]
        )
    with pytest.raises(MalformedClaimObservation):
        finalize_judgment_commit(prepared, [{"kind": "wrong"}])
    with pytest.raises(ClaimUnavailable):
        finalize_judgment_commit(
            prepared,
            [
                title_observation,
                UnavailableContentClaimObservation(
                    content_hash=content_hash,
                    reason="reserved",
                ),
            ],
        )


def test_finalize_rejects_committed_title_conflict() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    with pytest.raises(TitleClaimConflict):
        finalize_judgment_commit(
            prepared,
            [
                CommittedTitleClaimObservation(
                    normalized_title=prepared.claim_probes[0].normalized_title,
                    owner_decision_number=7,
                ),
                AbsentContentClaimObservation(content_hash=prepared.claim_probes[1].content_hash),
            ],
        )


def test_finalize_rejects_historical_content_and_wrong_owned_title() -> None:
    existing = _decision(1, "Existing decision")
    generation = _generation(existing, counter=1)
    target = existing[0].removeprefix("decisions/").removesuffix(".md")
    prepared = prepare_judgment_commit(
        _preteam_payload(generation, operation="update", target=target),
        _preteam_attestation(),
        generation,
    )
    title_probe, content_probe = prepared.claim_probes
    with pytest.raises(MismatchedClaimObservation):
        finalize_judgment_commit(
            prepared,
            [
                CommittedTitleClaimObservation(
                    normalized_title=title_probe.normalized_title,
                    owner_decision_number=2,
                ),
                AbsentContentClaimObservation(content_hash=content_probe.content_hash),
            ],
        )
    with pytest.raises(ContentClaimConflict, match="history"):
        finalize_judgment_commit(
            prepared,
            [
                CommittedTitleClaimObservation(
                    normalized_title=title_probe.normalized_title,
                    owner_decision_number=1,
                ),
                CommittedContentClaimObservation(content_hash=content_probe.content_hash),
            ],
        )


def test_plan_record_is_bounded_and_excludes_transient_bytes() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    plan = finalize_judgment_commit(
        prepared,
        [
            AbsentTitleClaimObservation(
                normalized_title=prepared.claim_probes[0].normalized_title
            ),
            AbsentContentClaimObservation(content_hash=prepared.claim_probes[1].content_hash),
        ],
    )
    record = json.loads(plan.plan_record_bytes)
    assert record["record_schema"] == "nauro.judgment_commit.plan_record.v1"
    assert record["approval_mode"] == "pre_team_session"
    assert "artifacts" not in record["artifact_inventory"]
    assert "payload_bytes" not in record
    assert "generation_id" not in record.get("primary_decision", {})
    assert prepared.payload_bytes not in plan.plan_record_bytes


def test_title_claim_probe_requires_canonical_normalized_text() -> None:
    with pytest.raises(ValidationError, match="normalized_title"):
        AbsentTitleClaimProbe(normalized_title="  Existing  Decision ")


def test_title_claim_observation_requires_canonical_normalized_text() -> None:
    with pytest.raises(ValidationError, match="normalized_title"):
        AbsentTitleClaimObservation(normalized_title="  Existing  Decision ")


def test_models_are_deeply_immutable_at_collection_boundaries() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    assert isinstance(prepared.planned_artifacts, tuple)
    with pytest.raises(ValidationError):
        prepared.new_decision_counter = 99


def test_result_materialization_cannot_diverge_from_prepared_or_final_plan() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    plan = finalize_judgment_commit(
        prepared,
        [
            AbsentTitleClaimObservation(
                normalized_title=prepared.claim_probes[0].normalized_title
            ),
            AbsentContentClaimObservation(content_hash=prepared.claim_probes[1].content_hash),
        ],
    )
    artifacts_before = prepared.planned_artifacts
    record_before = plan.plan_record_bytes
    digest_before = plan.plan_record_digest
    result_before = plan.committed_result.model_dump(mode="json")

    for field in ("similar_decisions", "touched_decisions", "resolved_questions"):
        sequence = getattr(plan.committed_result, field)
        with pytest.raises(TypeError, match="immutable"):
            sequence.append("tamper")

    assert prepared.planned_artifacts == artifacts_before
    assert plan.plan_record_bytes == record_before
    assert plan.plan_record_digest == digest_before
    assert prepared.committed_result.model_dump(mode="json") == result_before
    assert plan.committed_result.model_dump(mode="json") == result_before


def test_prepared_constructor_rejects_divergent_derived_snapshot() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    fields = {name: getattr(prepared, name) for name in PreparedJudgmentCommit.model_fields}
    fields["snapshot"] = PlannedSnapshot(content=b"{}")
    with pytest.raises(ValidationError, match="snapshot bytes"):
        PreparedJudgmentCommit(**fields)


def test_prepared_constructor_rejects_unknown_transformation_version() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    fields = {name: getattr(prepared, name) for name in PreparedJudgmentCommit.model_fields}
    fields["transformation_version"] = 2
    with pytest.raises(ValidationError, match="transformation_version"):
        PreparedJudgmentCommit(**fields)


def test_final_plan_constructor_rejects_tampered_record_bytes() -> None:
    generation = _generation(("project.md", b"# Test\n"))
    prepared = prepare_judgment_commit(
        _preteam_payload(generation), _preteam_attestation(), generation
    )
    plan = finalize_judgment_commit(
        prepared,
        [
            AbsentTitleClaimObservation(
                normalized_title=prepared.claim_probes[0].normalized_title
            ),
            AbsentContentClaimObservation(content_hash=prepared.claim_probes[1].content_hash),
        ],
    )
    fields = {name: getattr(plan, name) for name in plan.__class__.model_fields}
    fields["plan_record_bytes"] = b"{}"
    with pytest.raises(ValidationError, match="plan record bytes"):
        plan.__class__(**fields)
