"""Pydantic ``Result`` models returned by the operations kernel.

Each operation returns a per-operation ``*Result`` model so transports
shape responses from typed attributes rather than loosely-typed dicts.
``RelatedDecision`` and ``ErrorPayload`` are shared submodels reused by
multiple operations; per-operation ``Result`` models live alongside.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RelatedDecision(BaseModel):
    """A decision surfaced by retrieval as related to a proposed approach.

    The shape is the canonical D141 form: enriched with status/date and a
    rationale preview so any transport can render the same hit without
    re-fetching the underlying decision body.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    score: float
    status: str
    date: str
    rationale_preview: str


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

    On the success path ``related_decisions`` contains zero or more
    :class:`RelatedDecision` hits and ``assessment`` carries the
    deterministic human-readable summary. On the rejection path
    ``error`` is populated; ``related_decisions`` stays empty and
    ``assessment`` stays empty. ``store`` is not part of the model;
    transport adapters add it back at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    related_decisions: list[RelatedDecision] = Field(default_factory=list)
    assessment: str = ""
    error: ErrorPayload | None = None


class GetDecisionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_decision`.

    On the success path ``content`` holds the decision's markdown body.
    On the miss path ``error`` is populated with ``kind="error"``; the
    ``store`` field is not part of the model and is added by transport
    adapters at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class GetContextResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_context`.

    On the success path ``content`` holds the assembled L0/L1/L2 markdown
    payload. On the rejection path ``error`` is populated with
    ``kind="rejected"`` (invalid level); ``content`` stays unset. The
    ``store`` field is not part of the model; transport adapters add it
    back at serialization time. The result intentionally stays a single
    text field — the kernel-side ``build_l0/l1/l2`` already return
    assembled markdown, so structured sub-fields would duplicate that
    work without buying the surface anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class GetRawFileResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.get_raw_file`.

    On the success path ``content`` holds the file's text body. On the
    miss path ``error`` is populated with ``kind="error"``. The ``store``
    field is not part of the model; transport adapters add it back at
    serialization time. Hints such as ``available_files`` are not part of
    the kernel result either — they belong to the adapter since the Store
    protocol does not expose general file enumeration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    error: ErrorPayload | None = None


class DecisionSummary(BaseModel):
    """One row in :class:`ListDecisionsResult`.

    Carries the same row fields the pre-cutover ``tool_list_decisions``
    envelope exposed (``number``, ``title``, ``date``, ``status``,
    ``type``, ``confidence``). ``date`` and ``type`` stay optional so
    decisions written without those frontmatter fields still serialize;
    the adapter's ``exclude_none=True`` template choice drops the keys
    when they are unset.
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

    ``decisions`` carries the projected rows, sorted by decision number
    descending and truncated to the caller-supplied ``limit``. The
    ``store`` field is not part of the model; transport adapters add it
    back at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: list[DecisionSummary] = Field(default_factory=list)


class SearchHit(BaseModel):
    """One ranked row in :class:`SearchDecisionsResult`.

    Carries the BM25 row fields the pre-cutover ``tool_search_decisions``
    envelope exposed (``number``, ``title``, ``date``, ``status``,
    ``relevance_snippet``, ``score``). ``date`` and ``relevance_snippet``
    stay optional so decisions without a parsed date or without a snippet
    extraction still serialize; the adapter's ``exclude_none=True``
    template choice drops the keys when they are unset.
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

    On the success path ``results`` carries the ranked hits, sorted by
    BM25 score descending and truncated to the caller-supplied ``limit``.
    On the rejection path ``error`` is populated with ``kind="rejected"``
    (empty/whitespace query); ``results`` stays empty. The ``store``
    field is not part of the model; transport adapters add it back at
    serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[SearchHit] = Field(default_factory=list)
    error: ErrorPayload | None = None


class DiffSinceLastSessionResult(BaseModel):
    """Return shape for :func:`nauro_core.operations.diff_since_last_session`.

    ``diff`` carries the human-readable diff body. The "not enough
    snapshots" and "only one snapshot covers the requested range" cases
    populate ``diff`` with their respective sentinel strings rather than
    surfacing as errors — pre-cutover behaviour the surface tests pin.
    ``cutoff_date_used`` echoes the baseline snapshot timestamp when the
    adapter resolved the baseline via a time-based lookup; it stays
    unset for session-scoped diffs. ``store`` is not part of the model;
    transport adapters add it back at serialization time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    diff: str | None = None
    cutoff_date_used: str | None = None
    error: ErrorPayload | None = None
