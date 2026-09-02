"""Prepared local compare-and-swap commits for verified generation refreshes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import InitVar, dataclass, field

from nauro.store.generation_authority import (
    InstalledGenerationPointer,
    select_generation_refresh_base,
)
from nauro.store.generation_installation import (
    GenerationRefreshConflictError,
    GenerationRefreshError,
    _install_verified_generation_refresh,
)
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.replica_control import locked_replica_control_snapshot

_PREPARED_TOKEN = object()
_VERIFIED_TOKEN = object()


@dataclass(frozen=True, eq=False)
class PreparedGenerationRefresh:
    """An authenticated target fenced to the exact local pointer seen at entry."""

    target: GenerationProjectionTarget = field(repr=False)
    base_pointer: InstalledGenerationPointer = field(repr=False)
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if (
            _construction_token is not _PREPARED_TOKEN
            or type(self.target) is not GenerationProjectionTarget
            or type(self.base_pointer) is not InstalledGenerationPointer
        ):
            raise GenerationRefreshError(
                "Generation refreshes must be prepared from lock-held control state."
            )


@dataclass(frozen=True, eq=False)
class VerifiedGenerationRefresh:
    """A prepared refresh bound to its fully verified projection bytes."""

    prepared: PreparedGenerationRefresh = field(repr=False)
    projection: VerifiedGenerationProjection = field(repr=False)
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if (
            _construction_token is not _VERIFIED_TOKEN
            or type(self.prepared) is not PreparedGenerationRefresh
            or type(self.projection) is not VerifiedGenerationProjection
            or self.projection.target != self.prepared.target
        ):
            raise GenerationRefreshError(
                "Verified refresh bytes do not match their prepared target."
            )


def prepare_generation_refresh(
    target: GenerationProjectionTarget,
    *,
    lock_timeout: float = -1,
) -> PreparedGenerationRefresh:
    """Bind authenticated server target facts to the current local pointer."""
    if type(target) is not GenerationProjectionTarget:
        raise GenerationRefreshError(
            "Generation refresh preparation requires an authenticated target."
        )
    with locked_replica_control_snapshot(
        target.binding,
        active_user_id=target.identity.installed_for_user_id,
        timeout=lock_timeout,
    ) as snapshot:
        base_pointer = select_generation_refresh_base(
            target.binding,
            marker_json=snapshot.marker_json,
            pointer_json=snapshot.pointer_json,
            active_user_id=target.identity.installed_for_user_id,
        )
        return PreparedGenerationRefresh(target, base_pointer, _PREPARED_TOKEN)


def verify_generation_refresh(
    prepared: PreparedGenerationRefresh,
    *,
    manifest_json: bytes,
    artifacts: Sequence[tuple[str, bytes]],
) -> VerifiedGenerationRefresh:
    """Verify downloaded projection bytes against one prepared refresh target."""
    if type(prepared) is not PreparedGenerationRefresh:
        raise GenerationRefreshError("Generation refresh verification requires a prepared target.")
    projection = verify_generation_projection(
        prepared.target,
        manifest_json=manifest_json,
        artifacts=artifacts,
    )
    return VerifiedGenerationRefresh(prepared, projection, _VERIFIED_TOKEN)


def install_generation_refresh(
    refresh: VerifiedGenerationRefresh,
    *,
    lock_timeout: float = -1,
) -> InstalledGenerationPointer:
    """Commit verified bytes only if the prepared local pointer is still current."""
    if type(refresh) is not VerifiedGenerationRefresh:
        raise GenerationRefreshError("Generation refresh installation requires verified bytes.")
    return _install_verified_generation_refresh(
        refresh.projection,
        refresh.prepared.base_pointer,
        lock_timeout=lock_timeout,
    )


__all__ = [
    "GenerationRefreshConflictError",
    "GenerationRefreshError",
    "PreparedGenerationRefresh",
    "VerifiedGenerationRefresh",
    "install_generation_refresh",
    "prepare_generation_refresh",
    "verify_generation_refresh",
]
