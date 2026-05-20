"""Pydantic ``Result`` models returned by the operations kernel.

Each operation returns a per-operation ``*Result`` model so transports
shape responses from typed attributes rather than loosely-typed dicts.
PR 0 lands the ``RelatedDecision`` submodel; ``CheckDecisionResult`` and
``ErrorPayload`` ship with the ``check_decision`` operation cutover.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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
