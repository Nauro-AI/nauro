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


_FACADE_NAME_MANIFEST = (
    "AbsentContentClaimObservation",
    "AbsentContentClaimProbe",
    "AbsentTitleClaimObservation",
    "AbsentTitleClaimProbe",
    "Annotated",
    "ApprovalAttestation",
    "ApprovalAttestationRejected",
    "ApprovedBaseStale",
    "ApprovedPayload",
    "ApprovedPayloadDigestMismatch",
    "BaseModel",
    "CanonicalPayloadRejected",
    "ClaimIntent",
    "ClaimIntents",
    "ClaimObservation",
    "ClaimObservationError",
    "ClaimProbe",
    "ClaimUnavailable",
    "CommittedArtifact",
    "CommittedContentClaimObservation",
    "CommittedGeneration",
    "CommittedGenerationCorrupt",
    "CommittedTitleClaimObservation",
    "ConfigDict",
    "ContentClaimConflict",
    "Decision",
    "DecisionConfidence",
    "DecisionProvenance",
    "DecisionSource",
    "DecisionType",
    "DuplicateClaimObservation",
    "ErrorPayload",
    "Field",
    "FinalizedClaimPlan",
    "HostedPreTeamApprovalPayloadV1",
    "IdentifierKind",
    "JudgmentCommitError",
    "JudgmentCommitPlan",
    "JudgmentContent",
    "Literal",
    "MAX_RATIONALE_LENGTH",
    "MAX_TITLE_LENGTH",
    "MalformedClaimObservation",
    "Mapping",
    "MismatchedClaimObservation",
    "MissingClaimObservation",
    "OpenQuestionsFile",
    "OwnedTitleClaimProbe",
    "PlannedArtifact",
    "PlannedSnapshot",
    "PreTeamApprovalAttestation",
    "PreparedBase",
    "PreparedJudgmentCommit",
    "PrimaryDecision",
    "ProposalEvaluation",
    "ProposalRejected",
    "ProposeDecisionResult",
    "RejectedAlternative",
    "RejectedSelection",
    "RelatedDecision",
    "Reversibility",
    "Sequence",
    "StrictInt",
    "StrictStr",
    "TeamProposalPayloadV1",
    "TeamRatificationAttestation",
    "TitleClaimConflict",
    "TypeAdapter",
    "TypeAlias",
    "UnavailableContentClaimObservation",
    "UnavailableTitleClaimObservation",
    "UnexpectedClaimObservation",
    "ValidationError",
    "_CommittedResultProjection",
    "_FrozenModel",
    "_ImmutableResultList",
    "_OBSERVATION_ADAPTER",
    "_PAYLOAD_ADAPTER",
    "_SHA256_HEX_LENGTH",
    "_artifact_inventory",
    "_build_artifacts",
    "_claim_contract",
    "_decision_number_prefix",
    "_derive_snapshot_bytes",
    "_observation_key",
    "_parse_committed_generation",
    "_parse_payload",
    "_plan_record",
    "_probe_key",
    "_provenance",
    "_reject_duplicate_keys",
    "_rejected_result",
    "_sha256",
    "_target",
    "_validate_normalized_title",
    "_validate_observation",
    "_validate_sha256",
    "annotations",
    "append_decision_update",
    "attach_supersedes",
    "build_new_decision",
    "canonical_judgment_payload_bytes",
    "compute_hash",
    "computed_field",
    "date",
    "evaluate_parsed_proposal",
    "field_validator",
    "finalize_judgment_commit",
    "format_decision",
    "hashlib",
    "is_hosted_snapshot_content_member",
    "json",
    "mark_superseded",
    "model_validator",
    "normalize_title",
    "parse_decision",
    "prepare_judgment_commit",
    "resolve_questions_content",
    "serialize_snapshot",
    "slugify_decision_title",
    "validate_commit_sha",
    "validate_identifier",
    "validate_proposal_request",
    "validate_protected_generation_path",
    "validate_utc_timestamp",
)

_FACADE_DEFINED_CLASSES = (
    "AbsentContentClaimObservation",
    "AbsentContentClaimProbe",
    "AbsentTitleClaimObservation",
    "AbsentTitleClaimProbe",
    "ApprovalAttestationRejected",
    "ApprovedBaseStale",
    "ApprovedPayloadDigestMismatch",
    "CanonicalPayloadRejected",
    "ClaimIntent",
    "ClaimIntents",
    "ClaimObservationError",
    "ClaimUnavailable",
    "CommittedArtifact",
    "CommittedContentClaimObservation",
    "CommittedGeneration",
    "CommittedGenerationCorrupt",
    "CommittedTitleClaimObservation",
    "ContentClaimConflict",
    "DuplicateClaimObservation",
    "FinalizedClaimPlan",
    "HostedPreTeamApprovalPayloadV1",
    "JudgmentCommitError",
    "JudgmentCommitPlan",
    "JudgmentContent",
    "MalformedClaimObservation",
    "MismatchedClaimObservation",
    "MissingClaimObservation",
    "OwnedTitleClaimProbe",
    "PlannedArtifact",
    "PlannedSnapshot",
    "PreTeamApprovalAttestation",
    "PreparedBase",
    "PreparedJudgmentCommit",
    "PrimaryDecision",
    "ProposalRejected",
    "RejectedSelection",
    "TeamProposalPayloadV1",
    "TeamRatificationAttestation",
    "TitleClaimConflict",
    "UnavailableContentClaimObservation",
    "UnavailableTitleClaimObservation",
    "UnexpectedClaimObservation",
    "_CommittedResultProjection",
    "_FrozenModel",
    "_ImmutableResultList",
)

_FACADE_DEFINED_FUNCTIONS = (
    "_artifact_inventory",
    "_build_artifacts",
    "_claim_contract",
    "_derive_snapshot_bytes",
    "_observation_key",
    "_parse_committed_generation",
    "_parse_payload",
    "_plan_record",
    "_probe_key",
    "_provenance",
    "_reject_duplicate_keys",
    "_rejected_result",
    "_sha256",
    "_target",
    "_validate_normalized_title",
    "_validate_observation",
    "_validate_sha256",
    "canonical_judgment_payload_bytes",
    "finalize_judgment_commit",
    "prepare_judgment_commit",
)

_MOVED_FUNCTION_METADATA = {
    "_reject_duplicate_keys": ("(pairs: 'list[tuple[str, object]]') -> 'dict[str, object]'", None),
    "_parse_payload": ("(payload_bytes: 'bytes') -> 'ApprovedPayload'", None),
    "_parse_committed_generation": (
        "(generation: 'CommittedGeneration') -> "
        "'tuple[dict[str, bytes], list[Decision], dict[int, str]]'",
        None,
    ),
    "_rejected_result": (
        "(tier: 'int', operation: \"Literal['add', 'update', 'supersede']\", "
        "assessment: 'str') -> 'ProposalRejected'",
        None,
    ),
    "_target": (
        "(content: 'JudgmentContent', decisions: 'list[Decision]', "
        "stems_by_number: 'dict[int, str]') -> 'tuple[Decision | None, str | None]'",
        None,
    ),
    "_provenance": (
        "(payload: 'ApprovedPayload', attestation: 'ApprovalAttestation') -> "
        "'tuple[str, DecisionProvenance | None]'",
        None,
    ),
    "_derive_snapshot_bytes": (
        "(artifacts: 'tuple[PlannedArtifact, ...]', *, payload: 'ApprovedPayload', "
        "effective_at: 'str') -> 'bytes'",
        None,
    ),
    "_build_artifacts": (
        "(*, payload: 'ApprovedPayload', provenance: 'DecisionProvenance | None', "
        "effective_at: 'str', generation: 'CommittedGeneration', current: 'dict[str, bytes]', "
        "evaluation: 'ProposalEvaluation', target: 'Decision | None', "
        "target_stem: 'str | None') -> 'tuple[tuple[PlannedArtifact, ...], PlannedSnapshot, "
        "PrimaryDecision, ProposeDecisionResult, int | None, int, Decision]'",
        None,
    ),
    "_claim_contract": (
        "(operation: 'str', target: 'Decision | None', primary: 'Decision') -> "
        "'tuple[tuple[ClaimProbe, ...], ClaimIntents]'",
        None,
    ),
    "prepare_judgment_commit": (
        "(payload_bytes: 'bytes', approval_attestation: 'ApprovalAttestation', "
        "committed_generation: 'CommittedGeneration', expected_payload_digest: 'str | None' = "
        "None) -> 'PreparedJudgmentCommit'",
        "Prepare immutable artifacts and semantic claim reads without I/O.",
    ),
    "_probe_key": ("(probe: 'ClaimProbe') -> 'tuple[str, str]'", None),
    "_observation_key": (
        "(observation: 'ClaimObservation') -> 'tuple[str, str]'",
        None,
    ),
    "_validate_observation": (
        "(probe: 'ClaimProbe', observation: 'ClaimObservation') -> 'None'",
        None,
    ),
    "_artifact_inventory": (
        "(prepared: 'PreparedJudgmentCommit') -> 'dict[str, object]'",
        None,
    ),
    "_plan_record": (
        "(prepared: 'PreparedJudgmentCommit', observations: "
        "'tuple[ClaimObservation, ...]') -> 'bytes'",
        None,
    ),
    "finalize_judgment_commit": (
        "(prepared: 'PreparedJudgmentCommit', claim_observations: "
        "'Sequence[ClaimObservation | Mapping[str, object]]') -> 'JudgmentCommitPlan'",
        "Validate claim observations and finalize the storage-neutral plan.",
    ),
}


def _observations_for(prepared):
    import nauro_core.operations.commit_plan as contract

    observations = []
    for probe in prepared.claim_probes:
        if isinstance(probe, contract.AbsentTitleClaimProbe):
            observations.append(
                contract.AbsentTitleClaimObservation(normalized_title=probe.normalized_title)
            )
        elif isinstance(probe, contract.OwnedTitleClaimProbe):
            observations.append(
                contract.CommittedTitleClaimObservation(
                    normalized_title=probe.normalized_title,
                    owner_decision_number=probe.expected_owner_decision_number,
                )
            )
        else:
            observations.append(
                contract.AbsentContentClaimObservation(content_hash=probe.content_hash)
            )
    return observations


def test_commit_plan_facade_manifest_and_module_identity_are_stable() -> None:
    import inspect

    import nauro_core.operations.commit_plan as contract
    from nauro_core.operations import _commit_finalize, _commit_prepare

    assert tuple(sorted(name for name in vars(contract) if not name.startswith("__"))) == (
        _FACADE_NAME_MANIFEST
    )
    for name in _FACADE_DEFINED_CLASSES + _FACADE_DEFINED_FUNCTIONS:
        assert getattr(contract, name).__module__ == contract.__name__
    for private_module in (_commit_prepare, _commit_finalize):
        assert not [
            name
            for name, value in vars(private_module).items()
            if inspect.isclass(value) and value.__module__ == private_module.__name__
        ]


def test_commit_plan_moved_function_metadata_is_stable() -> None:
    import inspect

    import nauro_core.operations.commit_plan as contract

    actual = {
        name: (
            str(inspect.signature(getattr(contract, name))),
            inspect.getdoc(getattr(contract, name)),
        )
        for name in _MOVED_FUNCTION_METADATA
    }
    assert actual == _MOVED_FUNCTION_METADATA


def test_commit_plan_representative_pickle_identity_is_stable() -> None:
    import pickle

    import nauro_core.operations.commit_plan as contract

    values = (
        contract.PreparedJudgmentCommit,
        contract.JudgmentCommitPlan,
        contract.CanonicalPayloadRejected,
        contract.prepare_judgment_commit,
        contract.finalize_judgment_commit,
    )
    for value in values:
        assert pickle.loads(pickle.dumps(value)) is value


def _contract_json_value(value):
    import enum
    import math

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        assert math.isfinite(value)
        return value
    if isinstance(value, bytes):
        return {"kind": "bytes", "hex": value.hex()}
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum",
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _contract_json_value(value.value),
        }
    if isinstance(value, (list, tuple)):
        return [_contract_json_value(item) for item in value]
    if isinstance(value, dict):
        assert all(isinstance(key, str) for key in value)
        return {key: _contract_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, type):
        return {"kind": "type", "module": value.__module__, "qualname": value.__qualname__}
    raise AssertionError(f"unsupported contract value type: {type(value).__name__}")


def _contract_annotation_metadata(value):
    from annotated_types import Ge, Gt, Le, Lt, MaxLen, MinLen, MultipleOf
    from pydantic.fields import FieldInfo
    from pydantic.types import Strict

    if isinstance(value, FieldInfo):
        return {"kind": "field_info", "state": _contract_field_info_state(value)}
    constraint_types = (
        (Strict, "strict", "strict"),
        (Gt, "gt", "gt"),
        (Ge, "ge", "ge"),
        (Lt, "lt", "lt"),
        (Le, "le", "le"),
        (MinLen, "min_length", "min_length"),
        (MaxLen, "max_length", "max_length"),
        (MultipleOf, "multiple_of", "multiple_of"),
    )
    for metadata_type, kind, attribute in constraint_types:
        if isinstance(value, metadata_type):
            return {"kind": kind, "value": _contract_json_value(getattr(value, attribute))}
    raise AssertionError(f"unsupported annotation metadata: {type(value).__name__}")


def _contract_alias(value):
    from pydantic.aliases import AliasChoices, AliasPath

    if isinstance(value, str):
        return value
    if isinstance(value, AliasPath):
        return {"kind": "path", "path": [_contract_json_value(item) for item in value.path]}
    if isinstance(value, AliasChoices):
        return {
            "kind": "choices",
            "choices": [_contract_alias(choice) for choice in value.choices],
        }
    raise AssertionError(f"unsupported field alias: {type(value).__name__}")


def _contract_field_info_state(field):
    metadata = [_contract_annotation_metadata(value) for value in field.metadata]
    metadata.sort(
        key=lambda value: json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    state = {
        "metadata": metadata,
    }
    if field.discriminator is not None:
        assert isinstance(field.discriminator, str)
        state["discriminator"] = field.discriminator
    for name in ("alias", "validation_alias", "serialization_alias"):
        value = getattr(field, name)
        if value is not None:
            state[name] = _contract_alias(value)
    return state


def _contract_annotation(annotation):
    import types
    from typing import Annotated, Any, Literal, Union, get_args, get_origin

    if annotation is Any:
        return {"kind": "any"}
    if annotation is None:
        return {"kind": "none"}
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return {
            "kind": "annotated",
            "annotation": _contract_annotation(arguments[0]),
            "metadata": [_contract_annotation_metadata(value) for value in arguments[1:]],
        }
    if origin is Literal:
        return {
            "kind": "literal",
            "values": [_contract_json_value(value) for value in arguments],
        }
    if origin in (types.UnionType, Union):
        return {
            "kind": "union",
            "members": [_contract_annotation(value) for value in arguments],
        }
    if origin is not None:
        return {
            "kind": "generic",
            "origin": _contract_annotation(origin),
            "arguments": [_contract_annotation(value) for value in arguments],
        }
    if isinstance(annotation, type):
        return {
            "kind": "type",
            "module": annotation.__module__,
            "qualname": annotation.__qualname__,
        }
    if annotation is Ellipsis:
        return {"kind": "ellipsis"}
    raise AssertionError(f"unsupported contract annotation: {type(annotation).__name__}")


def _contract_default(field):
    if field.is_required():
        return {"kind": "required"}
    if field.default_factory is not None:
        factory = field.default_factory
        return {
            "kind": "factory",
            "module": factory.__module__,
            "qualname": factory.__qualname__,
        }
    return {"kind": "value", "value": _contract_json_value(field.default)}


def _facade_model_contract_projection(contract):
    from typing import get_type_hints

    from pydantic import BaseModel

    config_keys = (
        "extra",
        "from_attributes",
        "frozen",
        "populate_by_name",
        "strict",
        "use_enum_values",
        "validate_assignment",
        "validate_by_alias",
        "validate_by_name",
        "validate_default",
    )
    models = []
    for name, model in sorted(vars(contract).items()):
        if not (
            isinstance(model, type)
            and issubclass(model, BaseModel)
            and model.__module__ == contract.__name__
        ):
            continue
        type_hints = get_type_hints(model, include_extras=True)
        models.append(
            {
                "name": name,
                "module": model.__module__,
                "fields": [
                    {
                        "name": field_name,
                        "annotation": _contract_annotation(type_hints[field_name]),
                        "field_info": _contract_field_info_state(field),
                        "required": field.is_required(),
                        "default": _contract_default(field),
                    }
                    for field_name, field in model.model_fields.items()
                ],
                "model_config": {
                    key: _contract_json_value(model.model_config[key])
                    for key in config_keys
                    if key in model.model_config
                },
            }
        )
    return models


def test_commit_plan_facade_model_contract_digest_is_stable() -> None:
    import nauro_core.operations.commit_plan as contract

    projection = _facade_model_contract_projection(contract)
    projection_bytes = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert len(projection) == 28
    models = {model["name"]: model for model in projection}
    content_fields = {field["name"]: field for field in models["JudgmentContent"]["fields"]}
    rationale = content_fields["rationale"]
    assert rationale["annotation"] == {
        "kind": "annotated",
        "annotation": {"kind": "type", "module": "builtins", "qualname": "str"},
        "metadata": [{"kind": "strict", "value": True}],
    }
    generation_fields = {field["name"]: field for field in models["CommittedGeneration"]["fields"]}
    counter = generation_fields["decision_counter"]
    assert counter["annotation"] == {
        "kind": "annotated",
        "annotation": {"kind": "type", "module": "builtins", "qualname": "int"},
        "metadata": [{"kind": "strict", "value": True}],
    }
    assert {"kind": "strict", "value": True} in counter["field_info"]["metadata"]
    assert {"kind": "ge", "value": 0} in counter["field_info"]["metadata"]
    prepared_fields = {
        field["name"]: field for field in models["PreparedJudgmentCommit"]["fields"]
    }
    payload = prepared_fields["payload"]
    assert payload["field_info"]["discriminator"] == "payload_schema"
    assert payload["annotation"]["kind"] == "annotated"
    assert payload["annotation"]["annotation"]["kind"] == "union"
    assert payload["annotation"]["metadata"] == [
        {
            "kind": "field_info",
            "state": {"metadata": [], "discriminator": "payload_schema"},
        }
    ]
    assert hashlib.sha256(projection_bytes).hexdigest() == (
        "762393c65fc7098fb5835077de6a23815160845e02a5dd2ad9c2986bf0a8440a"
    )


@pytest.mark.parametrize("operation", ["add", "supersede"])
def test_private_implementation_matches_facade_for_two_phase_plans(operation: str) -> None:
    import nauro_core.operations.commit_plan as contract
    from nauro_core.operations import _commit_finalize, _commit_prepare

    if operation == "add":
        generation = _generation(("project.md", b"# Test\n"), counter=4)
        payload = _preteam_payload(generation)
    else:
        existing = _decision(3, "Existing decision")
        generation = _generation(existing, counter=8)
        target = existing[0].removeprefix("decisions/").removesuffix(".md")
        payload = _preteam_payload(
            generation,
            operation="supersede",
            target=target,
            title="Replacement decision",
        )
    attestation = _preteam_attestation()
    facade_prepared = contract.prepare_judgment_commit(payload, attestation, generation)
    private_prepared = _commit_prepare.prepare_judgment_commit_impl(
        payload,
        attestation,
        generation,
    )

    assert private_prepared == facade_prepared
    assert private_prepared.payload_bytes == facade_prepared.payload_bytes
    assert private_prepared.payload_digest == facade_prepared.payload_digest
    assert private_prepared.planned_artifacts == facade_prepared.planned_artifacts
    assert private_prepared.snapshot == facade_prepared.snapshot
    assert private_prepared.claim_probes == facade_prepared.claim_probes
    assert private_prepared.claim_intents == facade_prepared.claim_intents
    assert private_prepared.committed_result == facade_prepared.committed_result

    observations = _observations_for(facade_prepared)
    facade_plan = contract.finalize_judgment_commit(
        facade_prepared,
        list(reversed(observations)),
    )
    private_plan = _commit_finalize.finalize_judgment_commit_impl(
        private_prepared,
        list(reversed(observations)),
    )

    assert private_plan == facade_plan
    assert private_plan.validated_claim_observations == tuple(observations)
    assert private_plan.claim_plan == facade_plan.claim_plan
    assert private_plan.plan_record_bytes == facade_plan.plan_record_bytes
    assert private_plan.plan_record_digest == facade_plan.plan_record_digest
    assert private_plan.committed_result == facade_plan.committed_result


def test_prepare_validation_precedence_survives_delegation(monkeypatch) -> None:
    import nauro_core.operations.commit_plan as contract

    generation = _generation(("project.md", b"# Test\n"), counter=2)
    with pytest.raises(ApprovedPayloadDigestMismatch):
        contract.prepare_judgment_commit(
            b"not-json",
            _preteam_attestation(),
            generation,
            expected_payload_digest="f" * 64,
        )

    raw = json.loads(_preteam_payload(generation))
    raw["base_generation_id"] = PROPOSAL_ID
    monkeypatch.setattr(
        contract,
        "_parse_committed_generation",
        lambda value: pytest.fail("committed generation parsed before stale-base rejection"),
    )
    with pytest.raises(ApprovedBaseStale):
        contract.prepare_judgment_commit(
            canonical_judgment_payload_bytes(raw),
            _preteam_attestation(),
            generation,
        )


def test_finalize_validation_precedence_survives_delegation() -> None:
    import nauro_core.operations.commit_plan as contract

    generation = _generation(("project.md", b"# Test\n"))
    prepared = contract.prepare_judgment_commit(
        _preteam_payload(generation),
        _preteam_attestation(),
        generation,
    )
    title = prepared.claim_probes[0].normalized_title
    content_hash = prepared.claim_probes[1].content_hash
    title_observation = contract.AbsentTitleClaimObservation(normalized_title=title)
    duplicate = [
        title_observation,
        title_observation,
        contract.AbsentTitleClaimObservation(normalized_title="extra"),
    ]

    with pytest.raises(MalformedClaimObservation):
        contract.finalize_judgment_commit(
            prepared,
            [title_observation, title_observation, {"kind": "wrong"}],
        )
    with pytest.raises(DuplicateClaimObservation):
        contract.finalize_judgment_commit(prepared, duplicate)
    with pytest.raises(UnexpectedClaimObservation):
        contract.finalize_judgment_commit(
            prepared,
            [contract.AbsentTitleClaimObservation(normalized_title="extra")],
        )
    with pytest.raises(ClaimUnavailable):
        contract.finalize_judgment_commit(
            prepared,
            [
                contract.UnavailableTitleClaimObservation(
                    normalized_title=title,
                    reason="reserved",
                ),
                contract.CommittedContentClaimObservation(content_hash=content_hash),
            ],
        )
    with pytest.raises(TitleClaimConflict):
        contract.finalize_judgment_commit(
            prepared,
            [
                contract.CommittedTitleClaimObservation(
                    normalized_title=title,
                    owner_decision_number=7,
                ),
                contract.UnavailableContentClaimObservation(
                    content_hash=content_hash,
                    reason="reserved",
                ),
            ],
        )


@pytest.mark.parametrize(
    "module_order",
    [
        (
            "nauro_core.operations.commit_plan",
            "nauro_core.operations._commit_prepare",
            "nauro_core.operations._commit_finalize",
        ),
        (
            "nauro_core.operations._commit_prepare",
            "nauro_core.operations._commit_finalize",
            "nauro_core.operations.commit_plan",
        ),
    ],
)
def test_commit_plan_import_order_is_cycle_free_in_clean_subprocess(
    module_order: tuple[str, ...],
) -> None:
    import importlib
    import os
    import subprocess
    import sys

    code = (
        "import importlib, pickle\n"
        f"modules = [importlib.import_module(name) for name in {module_order!r}]\n"
        "contract = importlib.import_module('nauro_core.operations.commit_plan')\n"
        "assert contract.PreparedJudgmentCommit.__module__ == contract.__name__\n"
        "assert pickle.loads(pickle.dumps(contract.prepare_judgment_commit)) is "
        "contract.prepare_judgment_commit\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert importlib.import_module("nauro_core.operations.commit_plan").PreparedJudgmentCommit is (
        PreparedJudgmentCommit
    )
