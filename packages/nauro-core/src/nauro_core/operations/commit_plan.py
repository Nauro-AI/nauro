"""Pure two-phase planning for hosted judgment commits."""

# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

from nauro_core.constants import MAX_RATIONALE_LENGTH, MAX_TITLE_LENGTH
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
from nauro_core.identifiers import IdentifierKind, validate_identifier
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
from nauro_core.operations.results import ErrorPayload, ProposeDecisionResult, RelatedDecision
from nauro_core.parsing import _decision_number_prefix
from nauro_core.protected_generation_membership import (
    is_hosted_snapshot_content_member,
    validate_protected_generation_path,
)
from nauro_core.provenance import validate_commit_sha, validate_utc_timestamp
from nauro_core.questions import OpenQuestionsFile
from nauro_core.snapshot import serialize_snapshot
from nauro_core.validation import compute_hash, normalize_title

_SHA256_HEX_LENGTH = 64


class JudgmentCommitError(ValueError):
    """Base class for deterministic judgment-commit planning failures."""


class CanonicalPayloadRejected(JudgmentCommitError):
    """Approved payload bytes are invalid or noncanonical."""


class ApprovalAttestationRejected(JudgmentCommitError):
    """Approval attestation conflicts with the approved payload."""


class ApprovedPayloadDigestMismatch(JudgmentCommitError):
    """The checked expected digest does not match the approved bytes."""


class ApprovedBaseStale(JudgmentCommitError):
    """The approved base is not the observed committed generation."""


class CommittedGenerationCorrupt(JudgmentCommitError):
    """The committed artifact corpus violates generation invariants."""


class ProposalRejected(JudgmentCommitError):
    """Proposal evaluation produced the existing rejected result shape."""

    def __init__(self, result: ProposeDecisionResult) -> None:
        self.result = result
        super().__init__(result.assessment)


class ClaimObservationError(JudgmentCommitError):
    """Claim observations cannot finalize the prepared plan."""


class MissingClaimObservation(ClaimObservationError):
    pass


class UnexpectedClaimObservation(ClaimObservationError):
    pass


class DuplicateClaimObservation(ClaimObservationError):
    pass


class MalformedClaimObservation(ClaimObservationError):
    pass


class MismatchedClaimObservation(ClaimObservationError):
    pass


class ClaimUnavailable(ClaimObservationError):
    pass


class TitleClaimConflict(ClaimObservationError):
    pass


class ContentClaimConflict(ClaimObservationError):
    pass


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest.")
    if len(value) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest.")
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ImmutableResultList(list):
    def _reject_mutation(self, *args: object, **kwargs: object) -> None:
        raise TypeError("immutable result sequence")

    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    __setitem__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


class _CommittedResultProjection(_FrozenModel):
    status: Literal["confirmed", "rejected"]
    tier: StrictInt
    operation: Literal["add", "update", "supersede", "reject"]
    similar_decisions: tuple[RelatedDecision, ...]
    assessment: StrictStr
    decision_id: StrictStr | None
    touched_decisions: tuple[StrictStr, ...]
    resolved_questions: tuple[StrictStr, ...]
    relocated_ids: tuple[StrictStr, ...] | None
    skipped_prose_ids: tuple[StrictStr, ...] | None
    error: ErrorPayload | None

    @classmethod
    def from_result(cls, result: ProposeDecisionResult) -> _CommittedResultProjection:
        return cls.model_validate(result.model_dump())

    def materialize(self) -> ProposeDecisionResult:
        return ProposeDecisionResult.model_construct(
            status=self.status,
            tier=self.tier,
            operation=self.operation,
            similar_decisions=_ImmutableResultList(self.similar_decisions),
            assessment=self.assessment,
            decision_id=self.decision_id,
            touched_decisions=_ImmutableResultList(self.touched_decisions),
            resolved_questions=_ImmutableResultList(self.resolved_questions),
            relocated_ids=self.relocated_ids,
            skipped_prose_ids=self.skipped_prose_ids,
            error=self.error,
        )


class RejectedSelection(_FrozenModel):
    alternative: StrictStr
    reason: StrictStr

    @field_validator("alternative", "reason")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rejected alternative and reason must be non-empty.")
        return value


class JudgmentContent(_FrozenModel):
    operation: Literal["add", "update", "supersede"]
    affected_decision_id: StrictStr | None
    title: StrictStr | None
    rationale: StrictStr
    rejected: tuple[RejectedSelection, ...]
    confidence: Literal["high", "medium", "low"] | None
    decision_type: (
        Literal[
            "architecture", "api_design", "infrastructure", "pattern", "refactor", "data_model"
        ]
        | None
    )
    reversibility: Literal["easy", "moderate", "hard"] | None
    files_affected: tuple[StrictStr, ...]
    resolves_questions: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def _validate_operation_fields(self) -> JudgmentContent:
        if len(self.rationale) > MAX_RATIONALE_LENGTH:
            raise ValueError(f"rationale exceeds {MAX_RATIONALE_LENGTH} characters.")
        if len(set(self.resolves_questions)) != len(self.resolves_questions):
            raise ValueError("resolves_questions contains duplicate ids.")
        if any(not value for value in self.files_affected):
            raise ValueError("files_affected values must be non-empty strings.")
        if self.operation == "add":
            if self.affected_decision_id is not None:
                raise ValueError("add requires affected_decision_id to be null.")
        elif not self.affected_decision_id:
            raise ValueError(f"{self.operation} requires affected_decision_id.")
        if self.operation == "update":
            return self
        if self.title is not None and len(self.title) > MAX_TITLE_LENGTH:
            raise ValueError(f"title exceeds {MAX_TITLE_LENGTH} characters.")
        if self.confidence is None:
            raise ValueError(f"{self.operation} requires an effective confidence value.")
        return self


class HostedPreTeamApprovalPayloadV1(_FrozenModel):
    payload_schema: Literal["nauro.judgment_commit.pre_team.v1"]
    approval_mode: Literal["pre_team_session"]
    base_generation_id: StrictStr
    base_decision_counter: StrictInt = Field(ge=0)
    proposed_base_commit: None
    content: JudgmentContent

    @field_validator("base_generation_id")
    @classmethod
    def _generation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="base_generation_id")


class TeamProposalPayloadV1(_FrozenModel):
    payload_schema: Literal["nauro.judgment_commit.team_proposal.v1"]
    approval_mode: Literal["team_ratification"]
    proposal_id: StrictStr
    proposal_revision: StrictInt = Field(ge=1)
    proposed_by: StrictStr
    contributor_operation_id: StrictStr
    base_generation_id: StrictStr
    base_decision_counter: StrictInt = Field(ge=0)
    proposed_base_commit: StrictStr | None
    content: JudgmentContent

    @field_validator("proposal_id", "proposed_by", "base_generation_id")
    @classmethod
    def _ulids(cls, value: str, info) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field=info.field_name)

    @field_validator("contributor_operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(
            IdentifierKind.operation_id, value, field="contributor_operation_id"
        )

    @field_validator("proposed_base_commit")
    @classmethod
    def _base_commit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_commit_sha(value, field="proposed_base_commit")


ApprovedPayload: TypeAlias = Annotated[
    HostedPreTeamApprovalPayloadV1 | TeamProposalPayloadV1,
    Field(discriminator="payload_schema"),
]
_PAYLOAD_ADAPTER = TypeAdapter(ApprovedPayload)


class PreTeamApprovalAttestation(_FrozenModel):
    actor_id: StrictStr
    operation_id: StrictStr
    request_received_at: StrictStr

    @field_validator("actor_id")
    @classmethod
    def _actor_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="actor_id")

    @field_validator("operation_id")
    @classmethod
    def _operation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.operation_id, value, field="operation_id")

    @field_validator("request_received_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="request_received_at")


class TeamRatificationAttestation(_FrozenModel):
    approved_by: StrictStr
    approved_at: StrictStr
    ratification_action_id: StrictStr

    @field_validator("approved_by", "ratification_action_id")
    @classmethod
    def _ulids(cls, value: str, info) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field=info.field_name)

    @field_validator("approved_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return validate_utc_timestamp(value, field="approved_at")


ApprovalAttestation: TypeAlias = PreTeamApprovalAttestation | TeamRatificationAttestation


class CommittedArtifact(_FrozenModel):
    path: StrictStr
    content: bytes
    manifest_artifact_digest: StrictStr

    @model_validator(mode="after")
    def _validate_artifact(self) -> CommittedArtifact:
        validate_protected_generation_path(self.path)
        _validate_sha256(self.manifest_artifact_digest, field="manifest_artifact_digest")
        if _sha256(self.content) != self.manifest_artifact_digest:
            raise ValueError(f"artifact digest mismatch for {self.path!r}.")
        return self


class CommittedGeneration(_FrozenModel):
    generation_id: StrictStr
    decision_counter: StrictInt = Field(ge=0)
    observed_manifest_digest: StrictStr
    artifacts: tuple[CommittedArtifact, ...]

    @field_validator("generation_id")
    @classmethod
    def _generation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="generation_id")

    @field_validator("observed_manifest_digest")
    @classmethod
    def _manifest_digest(cls, value: str) -> str:
        return _validate_sha256(value, field="observed_manifest_digest")

    @model_validator(mode="after")
    def _unique_paths(self) -> CommittedGeneration:
        paths = [artifact.path for artifact in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("committed generation contains duplicate artifact paths.")
        return self


class PreparedBase(_FrozenModel):
    generation_id: StrictStr
    decision_counter: StrictInt = Field(ge=0)
    observed_manifest_digest: StrictStr

    @field_validator("generation_id")
    @classmethod
    def _generation_id(cls, value: str) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field="generation_id")

    @field_validator("observed_manifest_digest")
    @classmethod
    def _manifest_digest(cls, value: str) -> str:
        return _validate_sha256(value, field="observed_manifest_digest")


class PlannedArtifact(_FrozenModel):
    path: StrictStr
    content: bytes

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return validate_protected_generation_path(value)

    @computed_field
    @property
    def sha256(self) -> str:
        return _sha256(self.content)

    @computed_field
    @property
    def length(self) -> int:
        return len(self.content)


class PlannedSnapshot(_FrozenModel):
    content: bytes

    @computed_field
    @property
    def sha256(self) -> str:
        return _sha256(self.content)

    @computed_field
    @property
    def length(self) -> int:
        return len(self.content)


class PrimaryDecision(_FrozenModel):
    decision_id: StrictStr
    path: StrictStr
    sha256: StrictStr

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _validate_sha256(value, field="primary_decision.sha256")

    @model_validator(mode="after")
    def _path_matches_id(self) -> PrimaryDecision:
        if self.path != f"decisions/{self.decision_id}.md":
            raise ValueError("primary decision path must derive from decision_id.")
        return self


def _validate_normalized_title(value: str) -> str:
    if not value or normalize_title(value) != value:
        raise ValueError("normalized_title must be non-empty canonical normalized title text.")
    return value


class AbsentTitleClaimProbe(_FrozenModel):
    kind: Literal["absent_title"] = "absent_title"
    normalized_title: StrictStr

    _normalized = field_validator("normalized_title")(_validate_normalized_title)


class OwnedTitleClaimProbe(_FrozenModel):
    kind: Literal["owned_title"] = "owned_title"
    normalized_title: StrictStr
    expected_owner_decision_number: StrictInt = Field(gt=0)

    _normalized = field_validator("normalized_title")(_validate_normalized_title)


class AbsentContentClaimProbe(_FrozenModel):
    kind: Literal["absent_content"] = "absent_content"
    content_hash: StrictStr

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        return _validate_sha256(value, field="content_hash")


ClaimProbe: TypeAlias = Annotated[
    AbsentTitleClaimProbe | OwnedTitleClaimProbe | AbsentContentClaimProbe,
    Field(discriminator="kind"),
]


class AbsentTitleClaimObservation(_FrozenModel):
    kind: Literal["absent_title"] = "absent_title"
    normalized_title: StrictStr

    _normalized = field_validator("normalized_title")(_validate_normalized_title)


class CommittedTitleClaimObservation(_FrozenModel):
    kind: Literal["committed_title"] = "committed_title"
    normalized_title: StrictStr
    owner_decision_number: StrictInt = Field(gt=0)

    _normalized = field_validator("normalized_title")(_validate_normalized_title)


class UnavailableTitleClaimObservation(_FrozenModel):
    kind: Literal["unavailable_title"] = "unavailable_title"
    normalized_title: StrictStr
    reason: Literal["reserved", "recovery_required"]

    _normalized = field_validator("normalized_title")(_validate_normalized_title)


class AbsentContentClaimObservation(_FrozenModel):
    kind: Literal["absent_content"] = "absent_content"
    content_hash: StrictStr

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        return _validate_sha256(value, field="content_hash")


class CommittedContentClaimObservation(_FrozenModel):
    kind: Literal["committed_content"] = "committed_content"
    content_hash: StrictStr

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        return _validate_sha256(value, field="content_hash")


class UnavailableContentClaimObservation(_FrozenModel):
    kind: Literal["unavailable_content"] = "unavailable_content"
    content_hash: StrictStr
    reason: Literal["reserved", "recovery_required"]

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        return _validate_sha256(value, field="content_hash")


ClaimObservation: TypeAlias = Annotated[
    AbsentTitleClaimObservation
    | CommittedTitleClaimObservation
    | UnavailableTitleClaimObservation
    | AbsentContentClaimObservation
    | CommittedContentClaimObservation
    | UnavailableContentClaimObservation,
    Field(discriminator="kind"),
]
_OBSERVATION_ADAPTER = TypeAdapter(ClaimObservation)


class ClaimIntent(_FrozenModel):
    kind: Literal[
        "acquire_new_title",
        "hold_owned_title",
        "acquire_title_transfer",
        "acquire_new_content_hash",
        "commit_title_owner",
        "retain_title_owner",
        "release_title_owner",
        "transfer_title_owner",
        "commit_content_hash",
    ]
    normalized_title: StrictStr | None = None
    content_hash: StrictStr | None = None
    owner_decision_number: StrictInt | None = Field(default=None, gt=0)
    from_owner_decision_number: StrictInt | None = Field(default=None, gt=0)
    to_owner_decision_number: StrictInt | None = Field(default=None, gt=0)


class ClaimIntents(_FrozenModel):
    entry: tuple[ClaimIntent, ...]
    publication: tuple[ClaimIntent, ...]


class FinalizedClaimPlan(_FrozenModel):
    entry: tuple[ClaimIntent, ...]
    publication: tuple[ClaimIntent, ...]


class PreparedJudgmentCommit(_FrozenModel):
    schema_version: Literal[1] = 1
    transformation_version: Literal[1] = 1
    payload_bytes: bytes
    payload_digest: StrictStr
    payload: ApprovedPayload
    approval_attestation: ApprovalAttestation
    base: PreparedBase
    effective_at: StrictStr
    decision_provenance: DecisionProvenance | None
    assigned_decision_number: StrictInt | None = Field(default=None, gt=0)
    new_decision_counter: StrictInt = Field(ge=0)
    planned_artifacts: tuple[PlannedArtifact, ...]
    snapshot: PlannedSnapshot
    primary_decision: PrimaryDecision
    claim_probes: tuple[ClaimProbe, ...]
    claim_intents: ClaimIntents
    result_projection: _CommittedResultProjection

    @property
    def committed_result(self) -> ProposeDecisionResult:
        return self.result_projection.materialize()

    @model_validator(mode="after")
    def _cross_validate(self) -> PreparedJudgmentCommit:
        if _sha256(self.payload_bytes) != self.payload_digest:
            raise ValueError("payload digest does not derive from payload bytes.")
        if _parse_payload(self.payload_bytes) != self.payload:
            raise ValueError("parsed payload does not derive from payload bytes.")
        if (
            self.payload.base_generation_id != self.base.generation_id
            or self.payload.base_decision_counter != self.base.decision_counter
        ):
            raise ValueError("prepared base does not match the approved payload.")
        effective_at, provenance = _provenance(self.payload, self.approval_attestation)
        if effective_at != self.effective_at or provenance != self.decision_provenance:
            raise ValueError("approval attestation does not match effective provenance.")
        paths = [artifact.path for artifact in self.planned_artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("planned artifacts must be unique and path-sorted.")
        primary = next(
            (
                artifact
                for artifact in self.planned_artifacts
                if artifact.path == self.primary_decision.path
            ),
            None,
        )
        if primary is None or primary.sha256 != self.primary_decision.sha256:
            raise ValueError("primary decision does not match planned artifact bytes.")
        primary_model = parse_decision(
            primary.content.decode("utf-8"),
            self.primary_decision.path.removeprefix("decisions/"),
        )
        target_model = None
        if self.payload.content.affected_decision_id is not None:
            target_path = f"decisions/{self.payload.content.affected_decision_id}.md"
            target_artifact = next(
                (artifact for artifact in self.planned_artifacts if artifact.path == target_path),
                None,
            )
            if target_artifact is None:
                raise ValueError("affected decision is absent from planned artifacts.")
            target_model = parse_decision(
                target_artifact.content.decode("utf-8"),
                target_path.removeprefix("decisions/"),
            )
        probes, intents = _claim_contract(
            self.payload.content.operation,
            target_model,
            primary_model,
        )
        if probes != self.claim_probes or intents != self.claim_intents:
            raise ValueError("claim probes and intents do not derive from planned artifacts.")
        if (
            _derive_snapshot_bytes(
                self.planned_artifacts,
                payload=self.payload,
                effective_at=self.effective_at,
            )
            != self.snapshot.content
        ):
            raise ValueError("snapshot bytes do not derive from planned artifacts.")
        expected_counter = self.base.decision_counter + (
            1 if self.payload.content.operation in ("add", "supersede") else 0
        )
        if self.new_decision_counter != expected_counter:
            raise ValueError("new decision counter does not match the operation.")
        if self.payload.content.operation in ("add", "supersede"):
            if self.assigned_decision_number != expected_counter:
                raise ValueError("assigned number must equal counter plus one.")
        elif self.assigned_decision_number is not None:
            raise ValueError("update must not assign a decision number.")
        expected_touched = [self.primary_decision.decision_id]
        if self.payload.content.operation == "supersede":
            assert self.payload.content.affected_decision_id is not None
            expected_touched.append(self.payload.content.affected_decision_id)
        if (
            self.committed_result.status != "confirmed"
            or self.committed_result.tier != 2
            or self.committed_result.operation != self.payload.content.operation
            or self.committed_result.decision_id != self.primary_decision.decision_id
            or self.committed_result.touched_decisions != expected_touched
            or self.committed_result.error is not None
        ):
            raise ValueError("committed result does not match the planned transition.")
        return self


class JudgmentCommitPlan(_FrozenModel):
    schema_version: Literal[1] = 1
    prepared: PreparedJudgmentCommit
    validated_claim_observations: tuple[ClaimObservation, ...]
    claim_plan: FinalizedClaimPlan
    plan_record_bytes: bytes

    @computed_field
    @property
    def plan_record_digest(self) -> str:
        return _sha256(self.plan_record_bytes)

    @property
    def committed_result(self) -> ProposeDecisionResult:
        return self.prepared.committed_result

    @model_validator(mode="after")
    def _verify_record(self) -> JudgmentCommitPlan:
        if tuple(_observation_key(value) for value in self.validated_claim_observations) != tuple(
            _probe_key(value) for value in self.prepared.claim_probes
        ):
            raise ValueError("validated observations do not match prepared probe order.")
        for probe, observation in zip(
            self.prepared.claim_probes,
            self.validated_claim_observations,
            strict=True,
        ):
            _validate_observation(probe, observation)
        if self.claim_plan.entry != self.prepared.claim_intents.entry or (
            self.claim_plan.publication != self.prepared.claim_intents.publication
        ):
            raise ValueError("finalized claim plan does not derive from prepared intents.")
        if self.plan_record_bytes != _plan_record(
            self.prepared, self.validated_claim_observations
        ):
            raise ValueError("plan record bytes do not derive from the prepared plan.")
        return self


def canonical_judgment_payload_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize the only accepted approved payload byte representation."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    from . import _commit_prepare

    return _commit_prepare._reject_duplicate_keys_impl(pairs)


def _parse_payload(payload_bytes: bytes) -> ApprovedPayload:
    from . import _commit_prepare

    return _commit_prepare._parse_payload_impl(payload_bytes)


def _parse_committed_generation(
    generation: CommittedGeneration,
) -> tuple[dict[str, bytes], list[Decision], dict[int, str]]:
    from . import _commit_prepare

    return _commit_prepare._parse_committed_generation_impl(generation)


def _rejected_result(
    tier: int,
    operation: Literal["add", "update", "supersede"],
    assessment: str,
) -> ProposalRejected:
    from . import _commit_prepare

    return _commit_prepare._rejected_result_impl(tier, operation, assessment)


def _target(
    content: JudgmentContent,
    decisions: list[Decision],
    stems_by_number: dict[int, str],
) -> tuple[Decision | None, str | None]:
    from . import _commit_prepare

    return _commit_prepare._target_impl(content, decisions, stems_by_number)


def _provenance(
    payload: ApprovedPayload,
    attestation: ApprovalAttestation,
) -> tuple[str, DecisionProvenance | None]:
    from . import _commit_prepare

    return _commit_prepare._provenance_impl(payload, attestation)


def _derive_snapshot_bytes(
    artifacts: tuple[PlannedArtifact, ...],
    *,
    payload: ApprovedPayload,
    effective_at: str,
) -> bytes:
    from . import _commit_prepare

    return _commit_prepare._derive_snapshot_bytes_impl(
        artifacts,
        payload=payload,
        effective_at=effective_at,
    )


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
) -> tuple[
    tuple[PlannedArtifact, ...],
    PlannedSnapshot,
    PrimaryDecision,
    ProposeDecisionResult,
    int | None,
    int,
    Decision,
]:
    from . import _commit_prepare

    return _commit_prepare._build_artifacts_impl(
        payload=payload,
        provenance=provenance,
        effective_at=effective_at,
        generation=generation,
        current=current,
        evaluation=evaluation,
        target=target,
        target_stem=target_stem,
    )


def _claim_contract(
    operation: str,
    target: Decision | None,
    primary: Decision,
) -> tuple[tuple[ClaimProbe, ...], ClaimIntents]:
    from . import _commit_prepare

    return _commit_prepare._claim_contract_impl(operation, target, primary)


def prepare_judgment_commit(
    payload_bytes: bytes,
    approval_attestation: ApprovalAttestation,
    committed_generation: CommittedGeneration,
    expected_payload_digest: str | None = None,
) -> PreparedJudgmentCommit:
    """Prepare immutable artifacts and semantic claim reads without I/O."""
    from . import _commit_prepare

    return _commit_prepare.prepare_judgment_commit_impl(
        payload_bytes,
        approval_attestation,
        committed_generation,
        expected_payload_digest,
    )


def _probe_key(probe: ClaimProbe) -> tuple[str, str]:
    from . import _commit_finalize

    return _commit_finalize._probe_key_impl(probe)


def _observation_key(observation: ClaimObservation) -> tuple[str, str]:
    from . import _commit_finalize

    return _commit_finalize._observation_key_impl(observation)


def _validate_observation(probe: ClaimProbe, observation: ClaimObservation) -> None:
    from . import _commit_finalize

    return _commit_finalize._validate_observation_impl(probe, observation)


def _artifact_inventory(prepared: PreparedJudgmentCommit) -> dict[str, object]:
    from . import _commit_finalize

    return _commit_finalize._artifact_inventory_impl(prepared)


def _plan_record(
    prepared: PreparedJudgmentCommit,
    observations: tuple[ClaimObservation, ...],
) -> bytes:
    from . import _commit_finalize

    return _commit_finalize._plan_record_impl(prepared, observations)


def finalize_judgment_commit(
    prepared: PreparedJudgmentCommit,
    claim_observations: Sequence[ClaimObservation | Mapping[str, object]],
) -> JudgmentCommitPlan:
    """Validate claim observations and finalize the storage-neutral plan."""
    from . import _commit_finalize

    return _commit_finalize.finalize_judgment_commit_impl(prepared, claim_observations)
