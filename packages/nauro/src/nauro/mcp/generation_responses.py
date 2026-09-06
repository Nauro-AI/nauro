from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nauro_core.protected_generation_membership import (
    InvalidGenerationPath,
    validate_protected_generation_path,
)

from nauro.auth import ActiveUserReadError
from nauro.mcp import generation_reads as reads
from nauro.mcp.generation_reads import GenerationReadResult, _Result
from nauro.mcp.rendering import try_render_envelope
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_projection import GenerationProjectionTarget
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_refresh import _authorize, admit_generation_store
from nauro.sync.history_transport import HttpHistoryTransport
from nauro.sync.remote import TransferBoundaryError, TransferSession, resolve_api_url

_READ_FAILURES = (GenerationAuthorityError, TransferBoundaryError)


@dataclass(frozen=True)
class GenerationToolResponse:
    envelope: dict[str, object]
    text: str
    is_error: bool


def _error(reason: str, *, kind: str = "error") -> GenerationToolResponse:
    bounded = reason[:600]
    return GenerationToolResponse(
        {"store": "local", "error": {"kind": kind, "reason": bounded}},
        f"Error: {bounded}",
        True,
    )


def _unavailable() -> GenerationToolResponse:
    return _error("Generation read unavailable. Check authorization and explicit replica recovery.")


def _finish(
    binding: ResolvedProjectBinding,
    read: GenerationReadResult[_Result],
    tool_name: str,
    session: TransferSession | None,
    renderer_kwargs: dict[str, object] | None = None,
) -> GenerationToolResponse:
    payload = read.result.model_dump(mode="json", exclude_none=True)
    failure = payload.get("error")
    if isinstance(failure, dict):
        return _error(
            str(failure.get("reason", "Read failed.")), kind=str(failure.get("kind", "error"))
        )
    identity = read.projection
    target = GenerationProjectionTarget(binding, identity)
    envelope: dict[str, object] = {
        "store": "local",
        **payload,
        "project": {"id": identity.project_id, "name": identity.project_id},
        "read_authority": {
            "kind": "generation",
            "project_id": identity.project_id,
            "generation_id": identity.generation_id,
            "manifest_digest": identity.manifest_digest,
            "committed_at": identity.committed_at,
            "freshness": "authorized_at_read",
        },
    }
    rendered = try_render_envelope(tool_name, envelope, renderer_kwargs)
    if rendered.failure is not None or rendered.text is None:
        return _error("Generation response could not be rendered.")
    text = rendered.text
    _authorize(target, session)
    frame = (
        f"Generation: {identity.generation_id}. Committed: {identity.committed_at}.\n"
        "Authorization checked for this read."
    )
    return GenerationToolResponse(envelope, f"{text}\n\n{frame}" if text else frame, False)


def _raw_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise InvalidGenerationPath("Invalid generation path.")
    canonical = "/".join(part for part in path.split("/") if part not in ("", "."))
    canonical = validate_protected_generation_path(canonical)
    if canonical == "questions-provenance.json":
        raise InvalidGenerationPath("Generic provenance access is unavailable.")
    return canonical


def get_context(
    binding: ResolvedProjectBinding,
    level: int | str = "L0",
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    if isinstance(level, str):
        level = {"L0": 0, "L1": 1, "L2": 2}.get(level.upper(), -1)
    if type(level) is not int or level not in (0, 1, 2):
        return _error("Invalid level. Use L0, L1 or L2.", kind="rejected")
    try:
        read = reads.get_context(binding, level, actor=actor, session=session)
        return _finish(binding, read, "get_context", session, {"level": level})
    except _READ_FAILURES:
        return _unavailable()


def get_decision(
    binding: ResolvedProjectBinding,
    number: int,
    mode: Literal["header", "full"] = "full",
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    try:
        read = reads.get_decision(binding, number, mode, actor=actor, session=session)
        return _finish(binding, read, "get_decision", session, {"mode": mode})
    except _READ_FAILURES:
        return _unavailable()


def get_raw_file(
    binding: ResolvedProjectBinding,
    path: str,
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    try:
        canonical = _raw_path(path)
    except InvalidGenerationPath:
        return _error("Invalid or unavailable generation path.", kind="rejected")
    try:
        read = reads.get_raw_file(binding, canonical, actor=actor, session=session)
        return _finish(binding, read, "get_raw_file", session, {"path": canonical})
    except _READ_FAILURES:
        return _unavailable()


def list_decisions(
    binding: ResolvedProjectBinding,
    limit: int = 20,
    include_superseded: bool = False,
    *,
    actor: str,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    try:
        read = reads.list_decisions(
            binding, limit, include_superseded, actor=actor, session=session
        )
        return _finish(binding, read, "list_decisions", session)
    except _READ_FAILURES:
        return _unavailable()


def search_decisions(
    binding: ResolvedProjectBinding,
    query: str,
    limit: int = 10,
    include_superseded: bool = False,
    *,
    actor: str,
    use_embeddings: bool = False,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    try:
        read = reads.search_decisions(
            binding,
            query,
            limit,
            include_superseded,
            actor=actor,
            use_embeddings=use_embeddings,
            session=session,
        )
        return _finish(binding, read, "search_decisions", session, {"query": query})
    except _READ_FAILURES:
        return _unavailable()


def check_decision(
    binding: ResolvedProjectBinding,
    proposed_approach: str,
    context: str | None = None,
    *,
    actor: str,
    use_embeddings: bool = False,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    try:
        read = reads.check_decision(
            binding,
            proposed_approach,
            context,
            actor=actor,
            use_embeddings=use_embeddings,
            session=session,
        )
        return _finish(binding, read, "check_decision", session)
    except _READ_FAILURES:
        return _unavailable()


def diff_since_last_session(
    binding: ResolvedProjectBinding,
    days: int | None = None,
    *,
    actor: str,
    transport: HttpHistoryTransport | None = None,
    session: TransferSession | None = None,
) -> GenerationToolResponse:
    if transport is None:
        return _error("Generation history requires an explicit authenticated transport.")
    if days is not None and type(days) is not int:
        return _error("History days must be an integer.", kind="rejected")
    try:
        transport.require_binding(binding, resolve_api_url())
        store = admit_generation_store(binding, actor=actor, session=session)
        result = transport.fetch(store.target, days)
        identity = store.target.identity
        envelope: dict[str, object] = {
            "store": "local",
            "diff": result.diff,
            "project": {"id": identity.project_id, "name": identity.project_id},
            "read_authority": result.read_authority.model_dump(mode="json"),
        }
        if result.cutoff_date_used is not None:
            envelope["cutoff_date_used"] = result.cutoff_date_used
        final = admit_generation_store(binding, actor=actor, session=session)
        if final.target != store.target:
            return _unavailable()
        return GenerationToolResponse(envelope, result.text, False)
    except (GenerationAuthorityError, TransferBoundaryError, ActiveUserReadError, OSError):
        return _unavailable()
