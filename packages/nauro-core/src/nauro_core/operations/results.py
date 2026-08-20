"""Pydantic ``Result`` models returned by the operations kernel.

Each operation returns a per-operation ``*Result`` model so transports
shape responses from typed attributes rather than loosely-typed dicts.
``RelatedDecision`` and ``ErrorPayload`` are shared submodels reused by
multiple operations; per-operation ``Result`` models live alongside.

The plan-returning operations (``update_stack``, ``share_context``,
``submit_report``) are
executed by the hosted server, so their outcome models — the accepted,
conflict, and workflow shapes at the bottom of this module — are defined
here as the cross-surface contract even though the kernel never
constructs them itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nauro_core.identifiers import IdentifierKind, validate_identifier


class RelatedDecision(BaseModel):
    """A decision surfaced by retrieval as related to a proposed approach.

    The canonical retrieval-hit shape: enriched with the triage-header
    fields (status, date, type, confidence, supersession refs) and a
    rationale lede so any transport can render the same hit without
    re-fetching the underlying decision body.

    ``decision_type``, ``confidence``, ``supersedes``, and
    ``superseded_by`` stay optional so hits whose decision omits those
    frontmatter fields still serialize; the adapter's ``exclude_none=True``
    template choice drops the keys when they are unset (the
    :class:`DecisionSummary`/:class:`SearchHit` key-omission contract).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    score: float
    status: str
    date: str
    rationale_preview: str
    decision_type: str | None = None
    confidence: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None


class ErrorPayload(BaseModel):
    """Structured error envelope returned on rejection or operation failure.

    ``kind`` discriminates between caller-fixable rejections (input over
    length, malformed argument) and operation-side failures. ``guidance``
    carries an onboarding string when the rejection has a remedial action
    the caller can take.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rejected", "error"]
    reason: str
    guidance: str | None = None


class CheckDecisionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.check_decision`.

    ``assessment`` is declared first so the takeaway leads the serialized
    payload (Pydantic ``model_dump`` preserves declaration order), matching
    the rendered block's takeaway-first order. On the success path it carries
    the deterministic human-readable summary and ``related_decisions``
    contains zero or more :class:`RelatedDecision` hits. On the rejection
    path ``error`` is populated; ``assessment`` stays empty and
    ``related_decisions`` stays empty. ``store`` is not part of the model;
    transport adapters add it back at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessment: str = ""
    related_decisions: list[RelatedDecision] = Field(default_factory=list)
    error: ErrorPayload | None = None


class GetDecisionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_decision`.

    On success ``content`` holds the decision's markdown body; on a miss ``error``
    is populated with ``kind="error"``. ``store`` is not part of the model;
    transport adapters add it at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class GetContextResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_context`.

    On success ``content`` holds the assembled L0/L1/L2 markdown; on rejection
    ``error`` carries ``kind="rejected"`` and ``content`` stays unset. One text
    field only, since the builders already return assembled markdown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class GetRawFileResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_raw_file`.

    On success ``content`` holds the file's text body; on a miss ``error`` carries
    ``kind="error"``. Hints such as ``available_files`` belong to the adapter, as
    does ``store``, since the Store protocol exposes no file enumeration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class DecisionSummary(BaseModel):
    """One row in :class:`ListDecisionsResult`.

    The row fields are fixed, so the ``tool_list_decisions`` envelope stays
    byte-identical across surfaces. ``date`` and ``type`` stay optional so decisions
    without them still serialize; ``exclude_none=True`` drops the keys when unset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    date: str | None = None
    status: str
    type: str | None = None
    confidence: str


class ListDecisionsResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.list_decisions`.

    ``decisions`` carries the projected rows, sorted by number descending and
    truncated to ``limit``. ``store`` is not part of the model; transport adapters
    add it at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: list[DecisionSummary] = Field(default_factory=list)


class SearchHit(BaseModel):
    """One ranked row in :class:`SearchDecisionsResult`.

    The BM25 row fields are fixed, so the ``tool_search_decisions`` envelope stays
    byte-identical across surfaces. ``date`` and ``relevance_snippet`` stay optional
    so hits without them still serialize; ``exclude_none=True`` drops unset keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    date: str | None = None
    status: str
    relevance_snippet: str | None = None
    score: float


class SearchDecisionsResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.search_decisions`.

    On success ``results`` carries the ranked hits, sorted by BM25 score descending
    and truncated to ``limit``. An empty or whitespace query populates ``error``
    with ``kind="rejected"`` and leaves ``results`` empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[SearchHit] = Field(default_factory=list)
    error: ErrorPayload | None = None


class UpdateStateResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.update_state`.

    ``status="ok"`` means a new ``state_current.md`` body was written, with history
    appended when a prior body existed; ``status="noop"`` means no state file existed
    and the adapter skips snapshot and push. ``warning`` carries an overlap caution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "noop"] = "ok"
    warning: str | None = None
    error: ErrorPayload | None = None


class FlagQuestionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.flag_question`.

    ``status="ok"`` means a new ``Q###`` was appended and ``num`` carries it;
    ``status="rejected"`` means a target was already resolved, ``error`` names the
    resolving decision and ``num`` stays unset. The resolve path reports its own ids.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "rejected"] = "ok"
    num: int | None = None
    relocated_ids: tuple[str, ...] | None = None
    skipped_prose_ids: tuple[str, ...] | None = None
    error: ErrorPayload | None = None


class DiffSinceLastSessionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.diff_since_last_session`.

    ``diff`` carries the human-readable body, including the "not enough snapshots"
    sentinels, which are not errors. ``cutoff_date_used`` is set only for a
    time-based baseline lookup. Transport adapters add ``store`` back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    diff: str | None = None
    cutoff_date_used: str | None = None
    error: ErrorPayload | None = None


class ProposeDecisionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.propose_decision`.

    ``confirmed`` means the write executed: ``decision_id`` is the file stem,
    ``touched_decisions`` every file rewritten, ``similar_decisions`` advisory.
    ``rejected`` names the offending field, with ``error`` set on a half-state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["confirmed", "rejected"]
    tier: int
    operation: Literal["add", "update", "supersede", "reject"]
    similar_decisions: list[RelatedDecision] = Field(default_factory=list)
    assessment: str = ""
    decision_id: str | None = None
    touched_decisions: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    relocated_ids: tuple[str, ...] | None = None
    skipped_prose_ids: tuple[str, ...] | None = None
    error: ErrorPayload | None = None


class UpdateStackAccepted(BaseModel):
    """Committed outcome of a hosted ``update_stack`` operation.

    ``stack_revision`` is the revision the store carries after the write, the
    precondition for the next conditional update; ``previous_revision`` is what it
    replaced. ``receipt_id`` and ``event_id`` bind it to the receipt and audit event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    stack_revision: str
    previous_revision: str
    receipt_id: str
    event_id: str


class StackRevisionConflict(BaseModel):
    """A supplied ``expected_revision`` did not match the current stack.

    A terminal outcome, not an error: no canonical write occurred.
    ``current_revision`` (a hex digest or the absent token) is the
    precondition a fresh call must observe and supply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["stale_revision"] = "stale_revision"
    expected_revision: str
    current_revision: str


class StateRevisionConflict(BaseModel):
    """A supplied state revision did not match the observed state_current.md bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["stale_revision"] = "stale_revision"
    expected_revision: str
    current_revision: str


class ShareContextAccepted(BaseModel):
    """Committed outcome of a hosted ``share_context`` operation.

    ``path`` is the immutable brief's store-relative location, ``question_id`` the
    ``Q###`` of its discovery pointer, ``content_digest`` the SHA-256 of the stored
    bytes. ``receipt_id`` and ``event_id`` bind it to the receipt and question event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    path: str
    question_id: str
    event_id: str
    content_digest: str
    receipt_id: str


class SubmitReportAccepted(BaseModel):
    """Committed outcome of an original hosted ``submit_report`` operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: Literal["accepted"] = "accepted"
    report_id: str
    event_id: str
    body_digest: str
    receipt_id: str

    @field_validator("report_id", "event_id", "receipt_id")
    @classmethod
    def _ulids(cls, value: str, info) -> str:
        return validate_identifier(IdentifierKind.ulid, value, field=info.field_name)

    @field_validator("body_digest")
    @classmethod
    def _body_digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("body_digest must be a lowercase SHA-256 digest.")
        return value

    @model_validator(mode="after")
    def _original_identity(self) -> SubmitReportAccepted:
        if self.report_id != self.event_id:
            raise ValueError("report_id must equal event_id for an original report.")
        return self


class SlugConflict(BaseModel):
    """The requested brief slug is already taken.

    A terminal outcome, not an error: briefs are immutable, so a landed
    slug never frees up. ``suggested_slug`` is a fresh candidate the
    caller may retry with as a new operation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["slug_conflict"] = "slug_conflict"
    slug: str
    suggested_slug: str


class WorkflowExpired(BaseModel):
    """A resumed operation is past the retry horizon.

    The workflow named by ``operation_id`` can no longer complete; nothing
    was mutated by the resume. The caller starts over with a fresh
    operation id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["expired"] = "expired"
    operation_id: str


class WorkflowInProgress(BaseModel):
    """The operation's durable workflow is still active.

    A retryable outcome: the invocation exhausted its per-call budget
    without reaching a terminal state, and retrying the same
    ``operation_id`` resumes the work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["in_progress"] = "in_progress"
    operation_id: str
