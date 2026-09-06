from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    GenerationProjectAuthority,
    RefreshRequiredError,
    _parse_marker,
)
from nauro.store.generation_installation import (
    _build_carrier,
    _build_pointer,
    _layout,
    _require_active_actor,
    audit_generation_tree,
    install_generation_root,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.generation_read import _capture
from nauro.store.generation_refresh_intent import (
    RefreshIntent,
    decode_intent,
    encode_intent,
    require_predecessor,
)
from nauro.store.generation_refresh_io import (
    RefreshPaths,
    durable_replace,
    preserve_predecessor,
    read_evidence,
    refresh_paths,
    sync_file,
    sync_parents,
)
from nauro.store.generation_refresh_state import (
    GenerationRefreshEvidenceError,
    RefreshControlPair,
    _pointer,
)
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.replica_control import _native_control_lock, _validate_managed_path
from nauro.store.repo_config import generate_ulid
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_acquisition import (
    acquire_generation_projection,
    check_generation_projection,
)
from nauro.sync.remote import TransferSession


class GenerationRefreshDurabilityError(GenerationAuthorityError):
    code = "generation_refresh_unresolved"


@dataclass(frozen=True)
class PreparedGenerationRefresh:
    projection: VerifiedGenerationProjection
    marker: bytes
    pointer: bytes
    carrier: bytes
    prior_intent: bytes | None


@contextmanager
def _locked(binding: ResolvedProjectBinding, actor: str) -> Iterator[RefreshPaths]:
    paths = refresh_paths(binding, actor)
    lock_path = paths.store / ".replica-control.lock"
    _validate_managed_path(paths.store, lock_path)
    try:
        with _native_control_lock(paths.store, lock_path, -1):
            _require_active_actor(actor)
            yield paths
    except OSError as exc:
        raise GenerationRefreshDurabilityError("Refresh persistence requires recovery.") from exc


def _controls(paths: RefreshPaths) -> tuple[bytes, bytes, bytes]:
    raw = tuple(read_evidence(paths, path) for path in (paths.marker, paths.pointer, paths.carrier))
    marker, pointer, carrier = raw
    if marker is None or pointer is None or carrier is None:
        raise GenerationRefreshEvidenceError("Refresh control evidence is missing.")
    return marker, pointer, carrier


def _intent(paths: RefreshPaths) -> tuple[bytes, RefreshIntent]:
    raw = read_evidence(paths, paths.intent)
    if raw is None:
        raise RefreshRequiredError("Explicit refresh bootstrap or recovery is required.")
    intent = decode_intent(raw)
    if intent.predecessor_digest is not None:
        archived = read_evidence(
            paths, paths.history / f"{intent.predecessor_digest}.json", archive=True
        )
        if archived is None:
            raise GenerationRefreshEvidenceError("Refresh predecessor is missing.")
        require_predecessor(intent, archived)
    if intent.classify(*_controls(paths)) == "conflict":
        raise GenerationRefreshEvidenceError("Refresh controls conflict with retained evidence.")
    return raw, intent


def _target(binding: ResolvedProjectBinding, intent: RefreshIntent) -> GenerationProjectionTarget:
    pointer = _pointer(intent.target_pointer_json.encode())
    identity = GenerationProjectionIdentity.model_validate(
        {name: getattr(pointer, name) for name in GenerationProjectionIdentity.model_fields}
    )
    return GenerationProjectionTarget(binding, identity)


def _authorize(target: GenerationProjectionTarget, session: TransferSession | None) -> None:
    actor = target.identity.installed_for_user_id
    _require_active_actor(actor)
    current = check_generation_projection(target.binding, active_user_id=actor, session=session)
    _require_active_actor(actor)
    if current != target:
        raise RefreshRequiredError("The current authorized projection requires reconciliation.")


def _prepare(
    binding: ResolvedProjectBinding, actor: str, session: TransferSession | None, *, bootstrap: bool
) -> PreparedGenerationRefresh:
    with _locked(binding, actor) as paths:
        marker, pointer, carrier = _controls(paths)
        raw = read_evidence(paths, paths.intent)
        if bootstrap:
            if raw is not None:
                raise RefreshRequiredError("An existing refresh intent cannot be bootstrapped.")
            pair = RefreshControlPair(pointer, carrier)
            observed = _pointer(pair.pointer_json)
            if observed.installed_for_user_id != actor or observed.project_id != binding.project_id:
                raise GenerationRefreshEvidenceError(
                    "Bootstrap controls belong to another binding."
                )
        else:
            raw, _ = _intent(paths)
    projection = acquire_generation_projection(binding, active_user_id=actor, session=session)
    return PreparedGenerationRefresh(projection, marker, pointer, carrier, raw)


def prepare_initial_generation_refresh(
    binding: ResolvedProjectBinding, *, actor: str, session: TransferSession | None = None
) -> PreparedGenerationRefresh:
    # This entry requires an explicit operator bootstrap, never inference from a missing intent.
    return _prepare(binding, actor, session, bootstrap=True)


def prepare_generation_refresh(
    binding: ResolvedProjectBinding, *, actor: str, session: TransferSession | None = None
) -> PreparedGenerationRefresh:
    return _prepare(binding, actor, session, bootstrap=False)


def _new_intent(prepared: PreparedGenerationRefresh) -> RefreshIntent:
    target = prepared.projection.target
    actor = target.identity.installed_for_user_id
    state_id = generate_ulid()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pointer = _build_pointer(target, actor, target.identity.committed_at, now, state_id)
    carrier = _build_carrier(target, actor, target.identity.committed_at, state_id)
    prior = prepared.prior_intent
    return RefreshIntent(
        schema_version=1,
        kind="refresh" if prior is None else "reconcile",
        marker_json=prepared.marker.decode(),
        base_pointer_json=prepared.pointer.decode(),
        base_authorization_json=prepared.carrier.decode(),
        target_pointer_json=pointer.canonical_bytes().decode(),
        target_authorization_json=carrier.canonical_bytes().decode(),
        predecessor_digest=None if prior is None else hashlib.sha256(prior).hexdigest(),
    )


def _sync_target(paths: RefreshPaths, projection: VerifiedGenerationProjection) -> None:
    root = _layout(paths.store, projection.target).root_path
    audit_generation_tree(root, projection)
    files = [root / "manifest.json", *(root / "store" / a.path for a in projection.artifacts)]
    for path in files:
        sync_file(paths, path)
    for directory in sorted({path.parent for path in files}, key=lambda path: -len(path.parts)):
        sync_parents(paths, directory)
    audit_generation_tree(root, projection)


def _complete(
    paths: RefreshPaths,
    intent: RefreshIntent,
    projection: VerifiedGenerationProjection,
    session: TransferSession | None,
) -> GenerationSnapshotStore:
    if projection.target != _target(projection.target.binding, intent):
        raise GenerationRefreshEvidenceError("Refresh target differs from retained intent.")
    raw = encode_intent(intent)
    if _intent(paths)[0] != raw or intent.classify(*_controls(paths)) != "target_present":
        raise RefreshRequiredError("Explicit refresh recovery is required before admission.")
    _authorize(projection.target, session)
    _sync_target(paths, projection)
    for path in (paths.marker, paths.intent, paths.carrier, paths.pointer):
        sync_file(paths, path)
        sync_parents(paths, path.parent)
    if intent.predecessor_digest is not None:
        sync_file(paths, paths.history / f"{intent.predecessor_digest}.json")
        sync_parents(paths, paths.history)
    if _intent(paths)[0] != raw or intent.classify(*_controls(paths)) != "target_present":
        raise GenerationRefreshEvidenceError("Refresh evidence changed during completion.")
    _sync_target(paths, projection)
    _authorize(projection.target, session)
    if _intent(paths)[0] != raw or intent.classify(*_controls(paths)) != "target_present":
        raise GenerationRefreshEvidenceError("Refresh evidence changed before admission.")
    return GenerationSnapshotStore(projection)


def _resume(
    paths: RefreshPaths,
    intent: RefreshIntent,
    projection: VerifiedGenerationProjection,
    session: TransferSession | None,
) -> GenerationSnapshotStore:
    _authorize(projection.target, session)
    _sync_target(paths, projection)
    sync_file(paths, paths.marker)
    sync_file(paths, paths.intent)
    sync_parents(paths, paths.actor)
    if _intent(paths)[0] != encode_intent(intent):
        raise GenerationRefreshEvidenceError("Refresh intent changed before publication.")
    _authorize(projection.target, session)
    state = intent.classify(*_controls(paths))
    if state == "base_present":
        _require_active_actor(projection.target.identity.installed_for_user_id)
        durable_replace(paths, paths.carrier, intent.target_authorization_json.encode())
        state = intent.classify(*_controls(paths))
    if state == "carrier_published":
        sync_file(paths, paths.carrier)
        sync_parents(paths, paths.actor)
        _require_active_actor(projection.target.identity.installed_for_user_id)
        durable_replace(paths, paths.pointer, intent.target_pointer_json.encode())
    return _complete(paths, intent, projection, session)


def commit_generation_refresh(
    prepared: PreparedGenerationRefresh, *, session: TransferSession | None = None
) -> GenerationSnapshotStore:
    projection = verify_generation_projection(
        prepared.projection.target,
        manifest_json=prepared.projection.manifest_json,
        artifacts=tuple((a.path, a.content) for a in prepared.projection.artifacts),
    )
    target = projection.target
    actor = target.identity.installed_for_user_id
    _authorize(target, session)
    install_generation_root(projection)
    with _locked(target.binding, actor) as paths:
        if _controls(paths) != (prepared.marker, prepared.pointer, prepared.carrier):
            raise GenerationRefreshEvidenceError("The prepared refresh base is stale.")
        if read_evidence(paths, paths.intent) != prepared.prior_intent:
            raise GenerationRefreshEvidenceError("The prepared refresh intent is stale.")
        _authorize(target, session)
        _sync_target(paths, projection)
        for path in (paths.marker, paths.pointer, paths.carrier):
            sync_file(paths, path)
        sync_parents(paths, paths.actor)
        if prepared.prior_intent is not None:
            _, prior = _intent(paths)
            if _target(target.binding, prior) == target:
                return _resume(paths, prior, projection, session)
        intent = _new_intent(prepared)
        if prepared.prior_intent is not None:
            require_predecessor(intent, prepared.prior_intent)
            assert intent.predecessor_digest is not None
            preserve_predecessor(paths, intent.predecessor_digest, prepared.prior_intent)
        durable_replace(paths, paths.intent, encode_intent(intent))
        return _resume(paths, intent, projection, session)


def admit_generation_store(
    binding: ResolvedProjectBinding, *, actor: str, session: TransferSession | None = None
) -> GenerationSnapshotStore:
    with _locked(binding, actor) as paths:
        _, intent = _intent(paths)
        if intent.classify(*_controls(paths)) != "target_present":
            raise RefreshRequiredError("Explicit refresh recovery is required before admission.")
        target = _target(binding, intent)
        _authorize(target, session)
        authority = GenerationProjectAuthority(
            binding,
            _parse_marker(intent.marker_json),
            _pointer(intent.target_pointer_json.encode()),
        )
        projection = _capture(authority)
        return _complete(paths, intent, projection, session)


def recover_generation_refresh(
    binding: ResolvedProjectBinding, *, actor: str, session: TransferSession | None = None
) -> GenerationSnapshotStore:
    prepared = prepare_generation_refresh(binding, actor=actor, session=session)
    return commit_generation_refresh(prepared, session=session)
