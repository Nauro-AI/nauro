"""Finalization implementation for hosted judgment commit planning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from nauro_core.operations._commit_contract import (
    OBSERVATION_ADAPTER,
    ClaimObservation,
    DuplicateClaimObservation,
    FinalizedClaimPlan,
    JudgmentCommitPlan,
    MalformedClaimObservation,
    MissingClaimObservation,
    PreparedJudgmentCommit,
    UnexpectedClaimObservation,
    build_plan_record,
    observation_key,
    probe_key,
    validate_observation,
)


def finalize_judgment_commit(
    prepared: PreparedJudgmentCommit,
    claim_observations: Sequence[ClaimObservation | Mapping[str, object]],
) -> JudgmentCommitPlan:
    """Validate claim observations and finalize the storage-neutral plan."""
    parsed: list[ClaimObservation] = []
    for raw in claim_observations:
        try:
            parsed.append(OBSERVATION_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            raise MalformedClaimObservation("claim observation is malformed.") from exc
    observed_by_key: dict[tuple[str, str], ClaimObservation] = {}
    for observation in parsed:
        key = observation_key(observation)
        if key in observed_by_key:
            raise DuplicateClaimObservation(f"duplicate claim observation for {key!r}.")
        observed_by_key[key] = observation
    probe_keys = {probe_key(probe) for probe in prepared.claim_probes}
    extra = set(observed_by_key) - probe_keys
    if extra:
        raise UnexpectedClaimObservation(f"unexpected claim observation(s): {sorted(extra)!r}.")
    missing = probe_keys - set(observed_by_key)
    if missing:
        raise MissingClaimObservation(f"missing claim observation(s): {sorted(missing)!r}.")
    ordered = tuple(observed_by_key[probe_key(probe)] for probe in prepared.claim_probes)
    for probe, observation in zip(prepared.claim_probes, ordered, strict=True):
        validate_observation(probe, observation)
    claim_plan = FinalizedClaimPlan(
        entry=prepared.claim_intents.entry,
        publication=prepared.claim_intents.publication,
    )
    record_bytes = build_plan_record(prepared, ordered)
    return JudgmentCommitPlan(
        prepared=prepared,
        validated_claim_observations=ordered,
        claim_plan=claim_plan,
        plan_record_bytes=record_bytes,
    )
