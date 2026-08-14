"""Finalization implementation for hosted judgment commit planning."""

from __future__ import annotations

from . import commit_plan as contract


def _probe_key_impl(probe: contract.ClaimProbe) -> tuple[str, str]:
    if isinstance(probe, contract.AbsentContentClaimProbe):
        return ("content", probe.content_hash)
    return ("title", probe.normalized_title)


def _observation_key_impl(observation: contract.ClaimObservation) -> tuple[str, str]:
    if isinstance(
        observation,
        (
            contract.AbsentContentClaimObservation,
            contract.CommittedContentClaimObservation,
            contract.UnavailableContentClaimObservation,
        ),
    ):
        return ("content", observation.content_hash)
    return ("title", observation.normalized_title)


def _validate_observation_impl(
    probe: contract.ClaimProbe, observation: contract.ClaimObservation
) -> None:
    if isinstance(
        observation,
        (contract.UnavailableTitleClaimObservation, contract.UnavailableContentClaimObservation),
    ):
        raise contract.ClaimUnavailable(
            f"claim {contract._observation_key(observation)!r} is {observation.reason}."
        )
    if isinstance(probe, contract.AbsentTitleClaimProbe):
        if isinstance(observation, contract.AbsentTitleClaimObservation):
            return
        if isinstance(observation, contract.CommittedTitleClaimObservation):
            raise contract.TitleClaimConflict(
                "normalized title is already owned by decision "
                f"{observation.owner_decision_number}."
            )
    elif isinstance(probe, contract.OwnedTitleClaimProbe):
        if isinstance(observation, contract.CommittedTitleClaimObservation) and (
            observation.owner_decision_number == probe.expected_owner_decision_number
        ):
            return
        raise contract.MismatchedClaimObservation(
            "owned title claim does not match the expected committed owner."
        )
    elif isinstance(observation, contract.AbsentContentClaimObservation):
        return
    elif isinstance(observation, contract.CommittedContentClaimObservation):
        raise contract.ContentClaimConflict("content hash is already present in decision history.")
    raise contract.MismatchedClaimObservation(
        "claim observation does not match its prepared probe."
    )


def _artifact_inventory_impl(prepared: contract.PreparedJudgmentCommit) -> dict[str, object]:
    descriptors = [
        {"length": artifact.length, "path": artifact.path, "sha256": artifact.sha256}
        for artifact in prepared.planned_artifacts
    ]
    inventory = {"artifacts": descriptors, "schema": "nauro.artifact_inventory.v1"}
    inventory_bytes = contract.canonical_judgment_payload_bytes(inventory)
    return {
        "artifact_count": len(descriptors),
        "digest": contract._sha256(inventory_bytes),
        "schema": "nauro.artifact_inventory.v1",
        "total_byte_count": sum(descriptor["length"] for descriptor in descriptors),
    }


def _plan_record_impl(
    prepared: contract.PreparedJudgmentCommit,
    observations: tuple[contract.ClaimObservation, ...],
) -> bytes:
    content = prepared.payload.content
    provenance = (
        {
            "approved_at": prepared.decision_provenance.approved_at,
            "approved_by": prepared.decision_provenance.approved_by,
            "proposal_id": prepared.decision_provenance.proposal_id,
            "proposed_base_commit": prepared.decision_provenance.proposed_base_commit,
            "proposed_by": prepared.decision_provenance.proposed_by,
        }
        if prepared.decision_provenance is not None
        else None
    )
    record = {
        "affected_decision_id": content.affected_decision_id,
        "approval_mode": prepared.payload.approval_mode,
        "approval": prepared.approval_attestation.model_dump(mode="json"),
        "artifact_inventory": contract._artifact_inventory(prepared),
        "base": prepared.base.model_dump(mode="json"),
        "claim_plan": {
            "entry": [value.model_dump(mode="json") for value in prepared.claim_intents.entry],
            "observations": [value.model_dump(mode="json") for value in observations],
            "probes": [value.model_dump(mode="json") for value in prepared.claim_probes],
            "publication": [
                value.model_dump(mode="json") for value in prepared.claim_intents.publication
            ],
        },
        "committed_result": prepared.committed_result.model_dump(mode="json"),
        "decision_provenance": provenance,
        "new_decision_counter": prepared.new_decision_counter,
        "operation": content.operation,
        "payload_digest": prepared.payload_digest,
        "payload_schema": prepared.payload.payload_schema,
        "primary_decision": prepared.primary_decision.model_dump(mode="json"),
        "record_schema": "nauro.judgment_commit.plan_record.v1",
        "snapshot": {
            "length": prepared.snapshot.length,
            "sha256": prepared.snapshot.sha256,
        },
        "transform_version": prepared.transformation_version,
    }
    return contract.canonical_judgment_payload_bytes(record)


def finalize_judgment_commit_impl(
    prepared: contract.PreparedJudgmentCommit,
    claim_observations: contract.Sequence[
        contract.ClaimObservation | contract.Mapping[str, object]
    ],
) -> contract.JudgmentCommitPlan:
    """Validate claim observations and finalize the storage-neutral plan."""
    parsed: list[contract.ClaimObservation] = []
    for raw in claim_observations:
        try:
            parsed.append(contract._OBSERVATION_ADAPTER.validate_python(raw))
        except contract.ValidationError as exc:
            raise contract.MalformedClaimObservation("claim observation is malformed.") from exc
    observed_by_key: dict[tuple[str, str], contract.ClaimObservation] = {}
    for observation in parsed:
        key = contract._observation_key(observation)
        if key in observed_by_key:
            raise contract.DuplicateClaimObservation(f"duplicate claim observation for {key!r}.")
        observed_by_key[key] = observation
    probe_keys = {contract._probe_key(probe) for probe in prepared.claim_probes}
    extra = set(observed_by_key) - probe_keys
    if extra:
        raise contract.UnexpectedClaimObservation(
            f"unexpected claim observation(s): {sorted(extra)!r}."
        )
    missing = probe_keys - set(observed_by_key)
    if missing:
        raise contract.MissingClaimObservation(
            f"missing claim observation(s): {sorted(missing)!r}."
        )
    ordered = tuple(observed_by_key[contract._probe_key(probe)] for probe in prepared.claim_probes)
    for probe, observation in zip(prepared.claim_probes, ordered, strict=True):
        contract._validate_observation(probe, observation)
    claim_plan = contract.FinalizedClaimPlan(
        entry=prepared.claim_intents.entry,
        publication=prepared.claim_intents.publication,
    )
    record_bytes = contract._plan_record(prepared, ordered)
    return contract.JudgmentCommitPlan(
        prepared=prepared,
        validated_claim_observations=ordered,
        claim_plan=claim_plan,
        plan_record_bytes=record_bytes,
    )
