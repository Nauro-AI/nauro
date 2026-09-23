"""Read derived guidance through the existing generation admission boundary."""

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import httpx

from nauro.store.generation_projection import GenerationProjectionIdentity
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.read_authority import observe_generation_marker, require_legacy_context
from nauro.store.resolution import ResolvedProjectBinding, resolve_project_binding
from nauro.sync.generation_refresh import _authorize, admit_generation_store
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.remote import TransferBoundaryError

_T = TypeVar("_T")


def read_generation_guidance(
    store_path: Path,
    render: Callable[[GenerationSnapshotStore], _T],
    *,
    snapshot: GenerationSnapshotStore | None = None,
) -> tuple[_T, GenerationProjectionIdentity] | None:
    try:
        require_legacy_context(store_path)
    except PermissionError:
        pass
    else:
        if snapshot is not None:
            raise PermissionError("Generation guidance binding changed.")
        return None
    try:
        binding = _binding(store_path)
        with GenerationTransferSession(binding) as session:
            store = _select_snapshot(binding, session, snapshot)
            result = render(store)
            _authorize(store.target, session)
            session.credentials()
            return result, store.target.identity
    except (ValueError, OSError, httpx.HTTPError, TransferBoundaryError) as exc:
        raise PermissionError(
            "Generation guidance unavailable. "
            "Check authorization and explicitly refresh the replica."
        ) from exc


def _binding(store_path: Path) -> ResolvedProjectBinding:
    binding = resolve_project_binding(store_path.name, None, use_cwd=False)
    if binding.store_path != store_path or observe_generation_marker(binding) is None:
        raise ValueError("Guidance binding changed")
    return binding


def check_guidance_available(store_path: Path) -> None:
    read_generation_guidance(store_path, lambda store: None)


def generation_notice(identity: GenerationProjectionIdentity) -> str:
    return (
        f"Derived context from generation {identity.generation_id}, "
        f"committed {identity.committed_at}. "
        "Authorization was checked when this context was generated. "
        "Saved guidance does not prove "
        "current authorization or freshness. "
        "Use Nauro read tools to check current project judgment."
    )


def _select_snapshot(
    binding: ResolvedProjectBinding,
    session: GenerationTransferSession,
    snapshot: GenerationSnapshotStore | None,
) -> GenerationSnapshotStore:
    if snapshot is None:
        return admit_generation_store(binding, actor=session.actor, session=session)
    if snapshot.target.binding != binding:
        raise ValueError("Guidance snapshot belongs to another binding")
    session.require_binding(binding)
    _authorize(snapshot.target, session)
    return snapshot
