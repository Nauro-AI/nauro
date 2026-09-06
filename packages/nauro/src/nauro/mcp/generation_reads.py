from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from nauro_core import operations
from nauro_core.operations.results import (
    CheckDecisionResult,
    GetContextResult,
    GetDecisionResult,
    GetRawFileResult,
    ListDecisionsResult,
    SearchDecisionsResult,
)

from nauro.store.generation_projection import GenerationProjectionIdentity
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_refresh import _authorize, admit_generation_store
from nauro.sync.remote import TransferSession

_Result = TypeVar(
    "_Result",
    CheckDecisionResult,
    GetContextResult,
    GetDecisionResult,
    GetRawFileResult,
    ListDecisionsResult,
    SearchDecisionsResult,
)


@dataclass(frozen=True)
class GenerationReadResult(Generic[_Result]):
    projection: GenerationProjectionIdentity
    result: _Result


def _finish(
    store: GenerationSnapshotStore, result: _Result, session: TransferSession | None
) -> GenerationReadResult[_Result]:
    _authorize(store.target, session)
    return GenerationReadResult(store.target.identity, result)


def get_context(
    binding: ResolvedProjectBinding,
    level: int,
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationReadResult[GetContextResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    return _finish(store, operations.get_context(store, level), session)


def get_decision(
    binding: ResolvedProjectBinding,
    number: int,
    mode: Literal["header", "full"] = "full",
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationReadResult[GetDecisionResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    return _finish(store, operations.get_decision(store, number, mode), session)


def get_raw_file(
    binding: ResolvedProjectBinding,
    path: str,
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationReadResult[GetRawFileResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    return _finish(store, operations.get_raw_file(store, path), session)


def list_decisions(
    binding: ResolvedProjectBinding,
    limit: int = 20,
    include_superseded: bool = False,
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationReadResult[ListDecisionsResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    result = operations.list_decisions(store, limit, include_superseded)
    return _finish(store, result, session)


def search_decisions(
    binding: ResolvedProjectBinding,
    query: str,
    limit: int = 10,
    include_superseded: bool = False,
    *,
    actor: str,
    use_embeddings: bool = False,
    session: TransferSession | None = None,
) -> GenerationReadResult[SearchDecisionsResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    result = operations.search_decisions(
        store, query, limit, include_superseded, use_embeddings=use_embeddings
    )
    return _finish(store, result, session)


def check_decision(
    binding: ResolvedProjectBinding,
    proposed_approach: str,
    context: str | None = None,
    *,
    actor: str,
    use_embeddings: bool = False,
    session: TransferSession | None = None,
) -> GenerationReadResult[CheckDecisionResult]:
    store = admit_generation_store(binding, actor=actor, session=session)
    result = operations.check_decision(
        store, proposed_approach, context, use_embeddings=use_embeddings
    )
    return _finish(store, result, session)
