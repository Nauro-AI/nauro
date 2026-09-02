from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

import pydantic as pyd
from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.protected_generation_membership import (
    InvalidGenerationPath,
    validate_protected_generation_path,
)
from nauro_core.provenance import validate_utc_timestamp

from nauro.store.generation_authority import ClientUpgradeRequiredError, GenerationAuthorityError
from nauro.store.resolution import ResolvedProjectBinding

_CONFIG = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)
_IDENTITY_FIELDS = frozenset(
    "project_id store_format_version generation_id manifest_digest committed_at "
    "installed_for_user_id projection_class projection_scope_id".split()
)
_TARGET_FIELDS = frozenset({"binding", "identity"})
_BOUND = "project_id store_format_version generation_id projection_class projection_scope_id"
_BINDING_FIELDS = frozenset({"store_path", "project_id", "display_name", "mode", "server_url"})
_BAD_TARGET = "Generation verification requires a validated projection target."
_BAD_IDENTITY = "Generation projection targets require a validated projection identity."
_BAD_BINDING = "Generation projection targets require a validated cloud binding."
_BAD_MANIFEST = "The generation manifest is malformed."
_BAD_ARTIFACTS = "The generation artifact set is malformed."


class GenerationProjectionVerificationError(GenerationAuthorityError):
    code = "generation_verification_failed"


def _exact(value: object, info: pyd.ValidationInfo) -> object:
    if info.field_name == "artifacts" and (
        type(value) is not dict
        or any(type(key) is not str or type(item) is not str for key, item in value.items())
    ):
        raise ValueError("artifact entries must be exact strings")
    if info.field_name == "store_format_version" and type(value) is not int:
        raise ValueError("value has an invalid concrete type")
    if info.field_name not in ("artifacts", "store_format_version") and type(value) is not str:
        raise ValueError("value has an invalid concrete type")
    return value


def _ulid(value: str, info: pyd.ValidationInfo) -> str:
    assert info.field_name is not None
    return validate_identifier(IdentifierKind.ulid, value, field=info.field_name)


def _digest(value: str, info: pyd.ValidationInfo | None = None) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("value must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: str, info: pyd.ValidationInfo) -> str:
    assert info.field_name is not None
    return validate_utc_timestamp(value, field=info.field_name)


class GenerationProjectionIdentity(pyd.BaseModel):
    model_config = _CONFIG
    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)
    generation_id: pyd.StrictStr
    manifest_digest: pyd.StrictStr
    committed_at: pyd.StrictStr
    installed_for_user_id: pyd.StrictStr
    projection_class: Literal["viewer", "contributor_plus"]
    projection_scope_id: pyd.StrictStr

    _exact_types = pyd.field_validator("*", mode="before")(_exact)
    _validate_ulids = pyd.field_validator("project_id", "generation_id", "installed_for_user_id")(
        _ulid
    )
    _validate_digests = pyd.field_validator("manifest_digest", "projection_scope_id")(_digest)
    _validate_timestamp = pyd.field_validator("committed_at")(_timestamp)


def _stored(
    value: object,
    expected_type: type[object],
    fields: frozenset[str],
    message: str,
) -> dict[str, object]:
    if type(value) is not expected_type:
        raise GenerationProjectionVerificationError(message)
    state = object.__getattribute__(value, "__dict__")
    valid = type(state) is dict and all(type(key) is str for key in state) and set(state) == fields
    if fields == _IDENTITY_FIELDS:
        try:
            field_set = object.__getattribute__(value, "__pydantic_fields_set__")
            extra = object.__getattribute__(value, "__pydantic_extra__")
        except AttributeError:
            field_set, extra = None, ()
        exact_field_set = type(field_set) is set and all(type(key) is str for key in field_set)
        valid = valid and exact_field_set and field_set == fields and extra is None
    if not valid:
        raise GenerationProjectionVerificationError(message)
    return cast(dict[str, object], state)


def _rebuild_identity(value: object) -> GenerationProjectionIdentity:
    state = _stored(
        value,
        GenerationProjectionIdentity,
        _IDENTITY_FIELDS,
        _BAD_IDENTITY,
    )
    facts = {name: state[name] for name in _IDENTITY_FIELDS}
    try:
        return GenerationProjectionIdentity.model_validate(facts)
    except (pyd.ValidationError, ValueError, TypeError, RecursionError):
        pass
    raise GenerationProjectionVerificationError(_BAD_IDENTITY)


def _rebuild_binding(value: object) -> ResolvedProjectBinding:
    state = _stored(value, ResolvedProjectBinding, _BINDING_FIELDS, _BAD_BINDING)
    path, project, name, mode, server = (
        state[key] for key in ("store_path", "project_id", "display_name", "mode", "server_url")
    )
    if (
        type(path) is not type(Path())
        or type(project) is not str
        or type(name) is not str
        or type(mode) is not str
        or type(server) is not str
        or not name
        or mode != "cloud"
        or not server
    ):
        raise GenerationProjectionVerificationError(_BAD_BINDING)
    try:
        validate_identifier(IdentifierKind.ulid, project, field="project_id")
    except (TypeError, ValueError):
        project = ""
    if not project:
        raise GenerationProjectionVerificationError(_BAD_BINDING)
    return ResolvedProjectBinding(path, project, name, "cloud", server)


def _target_parts(value: object) -> tuple[ResolvedProjectBinding, GenerationProjectionIdentity]:
    state = _stored(value, GenerationProjectionTarget, _TARGET_FIELDS, _BAD_TARGET)
    binding = _rebuild_binding(state["binding"])
    identity = _rebuild_identity(state["identity"])
    if identity.project_id != binding.project_id:
        raise GenerationProjectionVerificationError(
            "The generation projection belongs to another project."
        )
    if identity.store_format_version != HOSTED_STORE_FORMAT_VERSION:
        raise ClientUpgradeRequiredError(
            "The generation projection uses an unsupported store format."
        )
    return binding, identity


@dataclass(frozen=True)
class GenerationProjectionTarget:
    binding: ResolvedProjectBinding
    identity: GenerationProjectionIdentity

    def __post_init__(self) -> None:
        binding, identity = _target_parts(self)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "identity", identity)


class GenerationProjectionManifest(pyd.BaseModel):
    model_config = _CONFIG
    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)
    generation_id: pyd.StrictStr
    projection_class: Literal["viewer", "contributor_plus"]
    projection_scope_id: pyd.StrictStr
    artifacts: Mapping[pyd.StrictStr, pyd.StrictStr]

    _exact_types = pyd.field_validator("*", mode="before")(_exact)
    _validate_ulids = pyd.field_validator("project_id", "generation_id")(_ulid)
    _validate_scope = pyd.field_validator("projection_scope_id")(_digest)

    @pyd.field_validator("artifacts")
    @classmethod
    def _validate_artifacts(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        result = {
            validate_protected_generation_path(path): _digest(digest)
            for path, digest in value.items()
        }
        return MappingProxyType(dict(sorted(result.items())))

    def canonical_bytes(self) -> bytes:
        body = self.model_dump(exclude={"artifacts"})
        body["artifacts"] = dict(self.artifacts)
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )


def _strict_json_preflight(raw: bytes) -> None:
    text = raw.decode("utf-8")
    if text.startswith("\ufeff"):
        raise ValueError("JSON byte order marks are not allowed")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON object key")
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON constant")

    json.loads(text, object_pairs_hook=object_from_pairs, parse_constant=reject_constant)


def _parse_manifest(
    target: GenerationProjectionTarget, raw: object
) -> GenerationProjectionManifest:
    if type(raw) is not bytes:
        raise GenerationProjectionVerificationError("The generation manifest must be exact bytes.")
    if hashlib.sha256(raw).hexdigest() != target.identity.manifest_digest:
        raise GenerationProjectionVerificationError("The generation manifest digest diverges.")
    try:
        _strict_json_preflight(raw)
        manifest = GenerationProjectionManifest.model_validate_json(raw)
        canonical = manifest.canonical_bytes()
    except (pyd.ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        canonical = None
    if canonical is None:
        raise GenerationProjectionVerificationError(_BAD_MANIFEST)
    if canonical != raw:
        raise GenerationProjectionVerificationError("The generation manifest is not canonical.")
    manifest_binding = tuple(getattr(manifest, field) for field in _BOUND.split())
    target_binding = tuple(getattr(target.identity, field) for field in _BOUND.split())
    if manifest_binding != target_binding:
        raise GenerationProjectionVerificationError(
            "The generation manifest does not match the projection target."
        )
    if manifest.projection_class == "viewer" and "questions-provenance.json" in manifest.artifacts:
        raise GenerationProjectionVerificationError(
            "Viewer generation projections cannot include questions-provenance.json."
        )
    return manifest


def _protected_path(value: str) -> str:
    try:
        return validate_protected_generation_path(value)
    except InvalidGenerationPath:
        pass
    raise GenerationProjectionVerificationError(
        "The generation contains an invalid protected artifact path."
    )


@dataclass(frozen=True)
class VerifiedGenerationArtifact:
    path: str
    content: bytes
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.path) is not str or type(self.content) is not bytes:
            raise GenerationProjectionVerificationError(_BAD_ARTIFACTS)
        path = _protected_path(self.path)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "digest", hashlib.sha256(self.content).hexdigest())


def _rebuild_artifacts(
    manifest: GenerationProjectionManifest, raw: object
) -> tuple[VerifiedGenerationArtifact, ...]:
    if type(raw) is not tuple:
        raise GenerationProjectionVerificationError(_BAD_ARTIFACTS)
    entries: list[tuple[str, bytes]] = []
    for entry in raw:
        if type(entry) is not tuple or len(entry) != 2:
            raise GenerationProjectionVerificationError(_BAD_ARTIFACTS)
        path, content = entry
        if type(path) is not str or type(content) is not bytes:
            raise GenerationProjectionVerificationError(_BAD_ARTIFACTS)
        entries.append((path, content))
    validated: list[tuple[str, bytes]] = []
    for path, content in entries:
        validated.append((_protected_path(path), content))
    paths = tuple(path for path, _content in validated)
    if len(paths) != len(set(paths)):
        raise GenerationProjectionVerificationError(
            "The generation contains duplicate artifact paths."
        )
    by_path = dict(validated)
    if set(by_path) != set(manifest.artifacts):
        raise GenerationProjectionVerificationError(
            "The generation artifact set differs from the manifest."
        )
    artifacts = tuple(VerifiedGenerationArtifact(path, by_path[path]) for path in sorted(by_path))
    for artifact in artifacts:
        if artifact.digest != manifest.artifacts[artifact.path]:
            raise GenerationProjectionVerificationError(
                f"The generation artifact digest diverges: {artifact.path}."
            )
    return artifacts


@dataclass(frozen=True, init=False)
class VerifiedGenerationProjection:
    target: GenerationProjectionTarget
    manifest_json: bytes
    artifacts: tuple[VerifiedGenerationArtifact, ...]
    manifest: GenerationProjectionManifest
    artifacts_by_path: Mapping[str, VerifiedGenerationArtifact]

    def __init__(
        self,
        target: GenerationProjectionTarget,
        manifest_json: bytes,
        artifacts: tuple[tuple[str, bytes], ...],
    ) -> None:
        binding, identity = _target_parts(target)
        rebuilt_target = GenerationProjectionTarget(binding, identity)
        manifest = _parse_manifest(rebuilt_target, manifest_json)
        rebuilt = _rebuild_artifacts(manifest, artifacts)
        object.__setattr__(self, "target", rebuilt_target)
        object.__setattr__(self, "manifest_json", manifest_json)
        object.__setattr__(self, "artifacts", rebuilt)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(
            self,
            "artifacts_by_path",
            MappingProxyType({artifact.path: artifact for artifact in rebuilt}),
        )


def verify_generation_projection(
    target: GenerationProjectionTarget,
    *,
    manifest_json: bytes,
    artifacts: tuple[tuple[str, bytes], ...],
) -> VerifiedGenerationProjection:
    return VerifiedGenerationProjection(target, manifest_json, artifacts)


__all__ = (
    "GenerationProjectionIdentity GenerationProjectionManifest GenerationProjectionTarget "
    "GenerationProjectionVerificationError VerifiedGenerationArtifact "
    "VerifiedGenerationProjection verify_generation_projection"
).split()
