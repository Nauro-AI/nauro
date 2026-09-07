from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from nauro_core.constants import DECISIONS_DIR, PROJECT_MD
from nauro_core.protected_generation_membership import (
    InvalidGenerationPath,
    validate_protected_generation_path,
)

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.generation_read import read_installed_generation
from nauro.store.resolution import ResolvedProjectBinding

_PROJECT_FRAME_READ_ONLY = (
    "project.md is read-only for generation projects. Preserve local edits in a separate "
    "quarantine directory outside the replica and sync roots. Do not copy them into the "
    "active generation or upload them through raw sync."
)


class GenerationStoreReadOnlyError(GenerationAuthorityError):
    code = "generation_store_read_only"


class GenerationStorePathError(GenerationAuthorityError):
    code = "generation_store_invalid_path"


@dataclass(frozen=True, init=False)
class GenerationSnapshotStore:
    target: GenerationProjectionTarget = field(repr=False)
    _contents: Mapping[str, str] = field(repr=False)

    def __init__(self, projection: VerifiedGenerationProjection) -> None:
        verified = verify_generation_projection(
            projection.target,
            manifest_json=projection.manifest_json,
            artifacts=tuple((artifact.path, artifact.content) for artifact in projection.artifacts),
        )
        contents = {
            artifact.path: artifact.content.decode("utf-8", errors="replace")
            for artifact in verified.artifacts
        }
        object.__setattr__(self, "target", verified.target)
        object.__setattr__(self, "_contents", MappingProxyType(contents))

    def read_file(self, path: str) -> str | None:
        try:
            canonical = validate_protected_generation_path(path)
        except InvalidGenerationPath as exc:
            raise GenerationStorePathError(
                "The requested path is outside the protected generation."
            ) from exc
        return self._contents.get(canonical)

    def write_file(self, path: str, content: str) -> None:
        if path == PROJECT_MD:
            raise GenerationStoreReadOnlyError(_PROJECT_FRAME_READ_ONLY)
        raise GenerationStoreReadOnlyError("Generation snapshots do not permit file writes.")

    def delete_file(self, path: str) -> None:
        if path == PROJECT_MD:
            raise GenerationStoreReadOnlyError(_PROJECT_FRAME_READ_ONLY)
        raise GenerationStoreReadOnlyError("Generation snapshots do not permit file deletion.")

    def list_decisions(self) -> list[str]:
        prefix = f"{DECISIONS_DIR}/"
        return sorted(
            path[len(prefix) : -len(".md")]
            for path in self._contents
            if path.startswith(prefix) and path.endswith(".md")
        )

    def read_decision(self, file_stem: str) -> str | None:
        return self.read_file(f"{DECISIONS_DIR}/{file_stem}.md")

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        return {stem: self.read_decision(stem) for stem in stems}


def capture_generation_store(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str | None,
    active_projection_scope_id: str | None,
    timeout: float = -1,
) -> GenerationSnapshotStore:
    projection = read_installed_generation(
        binding,
        active_user_id=active_user_id,
        active_projection_scope_id=active_projection_scope_id,
        timeout=timeout,
    )
    return GenerationSnapshotStore(projection)


__all__ = [
    "GenerationSnapshotStore",
    "GenerationStorePathError",
    "GenerationStoreReadOnlyError",
    "capture_generation_store",
]
