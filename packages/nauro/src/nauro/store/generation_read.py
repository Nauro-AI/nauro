from __future__ import annotations

from pathlib import Path

from nauro.store.generation_authority import GenerationAuthorityError, GenerationProjectAuthority
from nauro.store.generation_installation import (
    GenerationInstallError,
    _installed_file,
    _layout,
    _read_expected,
    _require_directory,
    audit_generation_tree,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    GenerationProjectionVerificationError,
    VerifiedGenerationProjection,
    _parse_manifest,
    verify_generation_projection,
)
from nauro.store.replica_control import _validate_managed_path, locked_replica_control_snapshot
from nauro.store.resolution import ResolvedProjectBinding

_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024


class GenerationReadError(GenerationAuthorityError):
    code = "generation_read_unavailable"


def _capture_file(store_path: Path, path: Path, limit: int) -> bytes:
    _validate_managed_path(store_path, path)
    identity, size = _installed_file(path, "selected artifact")
    if size > limit:
        raise GenerationReadError("The selected generation exceeds the capture limit.")
    content = _read_expected(path, size, identity, "selected artifact")
    _validate_managed_path(store_path, path)
    if len(content) != size or _installed_file(path, "selected artifact") != (identity, size):
        raise GenerationReadError("The selected generation changed during capture.")
    return content


def _capture(authority: GenerationProjectAuthority) -> VerifiedGenerationProjection:
    pointer = authority.pointer
    identity = GenerationProjectionIdentity.model_validate(
        {name: getattr(pointer, name) for name in GenerationProjectionIdentity.model_fields}
    )
    target = GenerationProjectionTarget(authority.binding, identity)
    store_path = target.binding.store_path
    root = _layout(store_path, target).root_path
    _require_directory(store_path, root, root=True)
    manifest_json = _capture_file(
        store_path, root / "manifest.json", min(_MAX_MANIFEST_BYTES, _MAX_CAPTURE_BYTES)
    )
    manifest = _parse_manifest(target, manifest_json)
    total = len(manifest_json)
    artifacts = []
    for path in sorted(manifest.artifacts):
        content = _capture_file(
            store_path, root / "store" / path, min(_MAX_ARTIFACT_BYTES, _MAX_CAPTURE_BYTES - total)
        )
        artifacts.append((path, content))
        total += len(content)
    projection = verify_generation_projection(
        target, manifest_json=manifest_json, artifacts=tuple(artifacts)
    )
    audit_generation_tree(root, projection)
    return projection


def read_installed_generation(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str | None,
    active_projection_scope_id: str | None,
    timeout: float = -1,
) -> VerifiedGenerationProjection:
    """Capture one complete immutable projection under its replica control lock."""
    with locked_replica_control_snapshot(
        binding,
        active_user_id=active_user_id,
        active_projection_scope_id=active_projection_scope_id,
        timeout=timeout,
    ) as snapshot:
        authority = snapshot.authority
        if not isinstance(authority, GenerationProjectAuthority):
            raise GenerationReadError("The project has no installed generation authority.")
        try:
            projection = _capture(authority)
        except (GenerationInstallError, GenerationProjectionVerificationError) as exc:
            raise GenerationReadError("The installed generation cannot be verified.") from exc
        if snapshot.reread_authority() != authority:
            raise GenerationReadError("The selected generation changed during capture.")
        return projection


__all__ = ["GenerationReadError", "read_installed_generation"]
