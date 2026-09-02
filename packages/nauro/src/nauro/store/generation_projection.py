"""Verification of downloaded hosted-generation projection bytes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import pydantic as pyd
from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.protected_generation_membership import (
    InvalidGenerationPath,
    validate_protected_generation_path,
)

from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    GenerationAuthorityError,
    GenerationProjectionIdentity,
)
from nauro.store.resolution import ResolvedProjectBinding

_STRICT_MODEL_CONFIG = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)


class GenerationProjectionVerificationError(GenerationAuthorityError):
    """Downloaded generation bytes do not match their committed authority."""

    code = "generation_verification_failed"


@dataclass(frozen=True)
class GenerationProjectionTarget:
    """Authenticated server facts that a downloaded projection must satisfy."""

    binding: ResolvedProjectBinding
    identity: GenerationProjectionIdentity

    def __post_init__(self) -> None:
        if type(self.binding) is not ResolvedProjectBinding or self.binding.mode != "cloud":
            raise GenerationProjectionVerificationError(
                "Generation projection targets require a validated cloud binding."
            )
        if type(self.identity) is not GenerationProjectionIdentity:
            raise GenerationProjectionVerificationError(
                "Generation projection targets require a validated projection identity."
            )
        if self.identity.project_id != self.binding.project_id:
            raise GenerationProjectionVerificationError(
                "The generation projection belongs to another project."
            )
        if self.identity.store_format_version != HOSTED_STORE_FORMAT_VERSION:
            raise ClientUpgradeRequiredError(
                "The generation projection uses an unsupported store format."
            )


def _ulid(value: str, info: pyd.ValidationInfo) -> str:
    field_name = info.field_name
    if field_name is None:
        raise ValueError("identifier validator requires a model field")
    return validate_identifier(IdentifierKind.ulid, value, field=field_name)


def _sha256(value: str, info: pyd.ValidationInfo) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
    return value


class GenerationProjectionManifest(pyd.BaseModel):
    """Canonical actor-scope binding envelope for one generation projection."""

    model_config = _STRICT_MODEL_CONFIG

    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)
    generation_id: pyd.StrictStr
    projection_class: pyd.StrictStr
    projection_scope_id: pyd.StrictStr
    artifacts: Mapping[pyd.StrictStr, pyd.StrictStr]

    _validate_ids = pyd.field_validator("project_id", "generation_id")(_ulid)
    _validate_projection_scope_id = pyd.field_validator("projection_scope_id")(_sha256)

    @pyd.field_validator("projection_class")
    @classmethod
    def _validate_projection_class(cls, value: str) -> str:
        if value not in ("viewer", "contributor_plus"):
            raise ValueError("projection_class is unsupported")
        return value

    @pyd.field_validator("artifacts")
    @classmethod
    def _validate_artifacts(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        validated: dict[str, str] = {}
        for path, digest in value.items():
            try:
                canonical_path = validate_protected_generation_path(path)
            except InvalidGenerationPath as exc:
                raise ValueError("manifest contains an invalid protected path") from exc
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(f"artifacts[{canonical_path}] must be a lowercase SHA-256 digest")
            validated[canonical_path] = digest
        return MappingProxyType(validated)

    def canonical_bytes(self) -> bytes:
        """Return the one persisted JSON representation of this manifest."""
        return json.dumps(
            {
                "project_id": self.project_id,
                "store_format_version": self.store_format_version,
                "generation_id": self.generation_id,
                "projection_class": self.projection_class,
                "projection_scope_id": self.projection_scope_id,
                "artifacts": dict(self.artifacts),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")


@dataclass(frozen=True)
class VerifiedGenerationArtifact:
    """One protected artifact with a digest derived from its exact bytes."""

    path: str
    content: bytes
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.path) is not str or type(self.content) is not bytes:
            raise GenerationProjectionVerificationError(
                "Generation artifacts require exact string paths and byte content."
            )
        try:
            path = validate_protected_generation_path(self.path)
        except InvalidGenerationPath as exc:
            raise GenerationProjectionVerificationError(
                "The generation contains an invalid protected artifact path."
            ) from exc
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "digest", hashlib.sha256(self.content).hexdigest())


class _DuplicateJsonKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def verify_generation_projection_manifest(
    target: GenerationProjectionTarget,
    manifest_json: bytes,
) -> GenerationProjectionManifest:
    """Verify one canonical manifest against authenticated projection identity."""
    if type(target) is not GenerationProjectionTarget:
        raise GenerationProjectionVerificationError(
            "Generation verification requires a validated projection target."
        )
    if type(manifest_json) is not bytes:
        raise GenerationProjectionVerificationError("The generation manifest must be exact bytes.")
    if hashlib.sha256(manifest_json).hexdigest() != target.identity.manifest_digest:
        raise GenerationProjectionVerificationError("The generation manifest digest diverges.")
    try:
        parsed: object = json.loads(
            manifest_json.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_invalid_json_constant,
        )
        manifest = GenerationProjectionManifest.model_validate(parsed)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        pyd.ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise GenerationProjectionVerificationError(
            "The generation manifest is malformed."
        ) from exc
    try:
        canonical_manifest = manifest.canonical_bytes()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise GenerationProjectionVerificationError(
            "The generation manifest is malformed."
        ) from exc
    if canonical_manifest != manifest_json:
        raise GenerationProjectionVerificationError("The generation manifest is not canonical.")
    expected_binding = (
        target.binding.project_id,
        target.identity.store_format_version,
        target.identity.generation_id,
        target.identity.projection_class,
        target.identity.projection_scope_id,
    )
    manifest_binding = (
        manifest.project_id,
        manifest.store_format_version,
        manifest.generation_id,
        manifest.projection_class,
        manifest.projection_scope_id,
    )
    if manifest_binding != expected_binding:
        raise GenerationProjectionVerificationError(
            "The generation manifest does not match the installed projection binding."
        )
    return manifest


@dataclass(frozen=True)
class VerifiedGenerationProjection:
    """A complete actor-bound projection verified against committed authority."""

    target: GenerationProjectionTarget
    manifest_json: bytes
    artifacts: tuple[VerifiedGenerationArtifact, ...]
    manifest: GenerationProjectionManifest = field(init=False)

    def __post_init__(self) -> None:
        if type(self.target) is not GenerationProjectionTarget:
            raise GenerationProjectionVerificationError(
                "Generation verification requires a validated projection target."
            )
        manifest = verify_generation_projection_manifest(self.target, self.manifest_json)
        if type(self.artifacts) is not tuple:
            raise GenerationProjectionVerificationError("The generation artifact set is malformed.")
        artifacts = self.artifacts
        if any(type(artifact) is not VerifiedGenerationArtifact for artifact in artifacts):
            raise GenerationProjectionVerificationError("The generation artifact set is malformed.")
        paths = tuple(artifact.path for artifact in artifacts)
        if len(paths) != len(set(paths)):
            raise GenerationProjectionVerificationError(
                "The generation contains duplicate artifact paths."
            )
        by_path = {artifact.path: artifact for artifact in artifacts}
        if set(by_path) != set(manifest.artifacts):
            raise GenerationProjectionVerificationError(
                "The generation artifact set differs from the manifest."
            )
        for path, expected_digest in manifest.artifacts.items():
            if by_path[path].digest != expected_digest:
                raise GenerationProjectionVerificationError(
                    f"The generation artifact digest diverges: {path}."
                )
        object.__setattr__(self, "artifacts", tuple(by_path[path] for path in sorted(by_path)))
        object.__setattr__(self, "manifest", manifest)

    @property
    def project_id(self) -> str:
        return self.target.binding.project_id

    @property
    def generation_id(self) -> str:
        return self.manifest.generation_id

    @property
    def manifest_digest(self) -> str:
        return self.target.identity.manifest_digest

    @property
    def total_bytes(self) -> int:
        return sum(len(artifact.content) for artifact in self.artifacts)


def verify_generation_projection(
    target: GenerationProjectionTarget,
    *,
    manifest_json: bytes,
    artifacts: Sequence[tuple[str, bytes]],
) -> VerifiedGenerationProjection:
    """Verify exact downloaded bytes without reading the network or local store."""
    verified_artifacts: list[VerifiedGenerationArtifact] = []
    try:
        for entry in artifacts:
            if type(entry) is not tuple or len(entry) != 2:
                raise TypeError("artifact entry must be a path and content tuple")
            verified_artifacts.append(VerifiedGenerationArtifact(entry[0], entry[1]))
    except GenerationProjectionVerificationError:
        raise
    except (TypeError, ValueError) as exc:
        raise GenerationProjectionVerificationError(
            "The generation artifact set is malformed."
        ) from exc
    return VerifiedGenerationProjection(
        target=target,
        manifest_json=manifest_json,
        artifacts=tuple(verified_artifacts),
    )


__all__ = [
    "GenerationProjectionManifest",
    "GenerationProjectionTarget",
    "GenerationProjectionVerificationError",
    "VerifiedGenerationArtifact",
    "VerifiedGenerationProjection",
    "verify_generation_projection",
    "verify_generation_projection_manifest",
]
