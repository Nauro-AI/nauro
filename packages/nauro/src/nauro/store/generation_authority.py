"""Strict local authority selection for hosted generation replicas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import pydantic as pyd
from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.provenance import validate_utc_timestamp

from nauro.store.resolution import ResolvedProjectBinding

GENERATION_CONTROL_SCHEMA_VERSION = 1
_STRICT_MODEL_CONFIG = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)


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


def _load_strict_json(raw: str | bytes) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_invalid_json_constant,
    )


class GenerationAuthorityError(ValueError):
    """Base class for generation authority selection failures."""

    code: str


class GenerationControlCorruptError(GenerationAuthorityError):
    """Generation control evidence is malformed or cross-bound."""

    code = "generation_control_corrupt"


class ClientUpgradeRequiredError(GenerationAuthorityError):
    """The selected hosted store format is not supported by this client."""

    code = "client_upgrade_required"


class ReplicaActorMismatchError(GenerationAuthorityError):
    """The installed replica belongs to a different or unavailable actor."""

    code = "replica_actor_mismatch"


class RefreshRequiredError(GenerationAuthorityError):
    """The active authorization view does not match the installed projection."""

    code = "refresh_required"


def _ulid(value: str, info: pyd.ValidationInfo) -> str:
    field = info.field_name
    if field is None:
        raise ValueError("identifier validator requires a model field")
    return validate_identifier(IdentifierKind.ulid, value, field=field)


def _sha256(value: str, info: pyd.ValidationInfo) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: str, info: pyd.ValidationInfo) -> str:
    field = info.field_name
    if field is None:
        raise ValueError("timestamp validator requires a model field")
    return validate_utc_timestamp(value, field=field)


class GenerationAuthorityMarker(pyd.BaseModel):
    """The immutable local marker that selects generation authority."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: pyd.StrictInt
    authority: Literal["generation"]
    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)

    _validate_project_id = pyd.field_validator("project_id")(_ulid)

    @pyd.field_validator("schema_version")
    @classmethod
    def _supported_schema(cls, value: int) -> int:
        if value != GENERATION_CONTROL_SCHEMA_VERSION:
            raise ValueError("unsupported generation control schema")
        return value


class GenerationProjectionIdentity(pyd.BaseModel):
    """Server-authenticated identity of one actor-scoped generation projection."""

    model_config = _STRICT_MODEL_CONFIG

    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)
    generation_id: pyd.StrictStr
    manifest_digest: pyd.StrictStr
    committed_at: pyd.StrictStr
    installed_for_user_id: pyd.StrictStr
    projection_class: Literal["viewer", "contributor_plus"]
    projection_scope_id: pyd.StrictStr

    _validate_ulids = pyd.field_validator(
        "project_id",
        "generation_id",
        "installed_for_user_id",
    )(_ulid)
    _validate_digests = pyd.field_validator(
        "manifest_digest",
        "projection_scope_id",
    )(_sha256)
    _validate_committed_at = pyd.field_validator("committed_at")(_timestamp)


class InstalledGenerationPointer(GenerationProjectionIdentity):
    """The actor-bound pointer for one verified installed generation."""

    schema_version: pyd.StrictInt
    installed_at: pyd.StrictStr
    installed_state_id: pyd.StrictStr

    _validate_installed_state_id = pyd.field_validator("installed_state_id")(_ulid)
    _validate_installed_at = pyd.field_validator("installed_at")(_timestamp)

    @pyd.field_validator("schema_version")
    @classmethod
    def _supported_schema(cls, value: int) -> int:
        if value != GENERATION_CONTROL_SCHEMA_VERSION:
            raise ValueError("unsupported generation control schema")
        return value


def projection_identity_from_pointer(
    pointer: InstalledGenerationPointer,
) -> GenerationProjectionIdentity:
    """Return the server-authenticated identity fields from a validated pointer."""
    if type(pointer) is not InstalledGenerationPointer:
        raise GenerationControlCorruptError("The installed generation pointer is invalid.")
    values = {
        field_name: getattr(pointer, field_name)
        for field_name in GenerationProjectionIdentity.model_fields
    }
    return GenerationProjectionIdentity.model_validate(values)


@dataclass(frozen=True)
class FlatProjectAuthority:
    """A local-only or pre-epoch hosted flat store."""

    binding: ResolvedProjectBinding

    @property
    def kind(self) -> Literal["local", "hosted_legacy"]:
        return "local" if self.binding.mode == "local" else "hosted_legacy"


@dataclass(frozen=True)
class GenerationProjectAuthority:
    """A validated actor-bound hosted generation selection."""

    binding: ResolvedProjectBinding
    marker: GenerationAuthorityMarker
    pointer: InstalledGenerationPointer

    @property
    def kind(self) -> Literal["hosted_generation"]:
        return "hosted_generation"


ProjectAuthority = FlatProjectAuthority | GenerationProjectAuthority


def _parse_marker(raw: str | bytes) -> GenerationAuthorityMarker:
    if type(raw) not in (str, bytes):
        raise GenerationControlCorruptError("The generation authority marker is invalid.")
    try:
        return GenerationAuthorityMarker.model_validate(_load_strict_json(raw))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        pyd.ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        raise GenerationControlCorruptError("The generation authority marker is invalid.") from exc


def _parse_pointer(raw: str | bytes) -> InstalledGenerationPointer:
    if type(raw) not in (str, bytes):
        raise GenerationControlCorruptError("The installed generation pointer is invalid.")
    try:
        return InstalledGenerationPointer.model_validate(_load_strict_json(raw))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        pyd.ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        raise GenerationControlCorruptError("The installed generation pointer is invalid.") from exc


def _active_user_id(value: str | None) -> str:
    try:
        return validate_identifier(IdentifierKind.ulid, value, field="active_user_id")
    except ValueError as exc:
        raise ReplicaActorMismatchError(
            "The installed replica does not match an active account."
        ) from exc


def _active_projection_scope_id(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise RefreshRequiredError("The installed replica requires a fresh authorization view.")
    return value


def _select_actor_generation_authority(
    binding: ResolvedProjectBinding,
    *,
    marker_json: str | bytes | None,
    pointer_json: str | bytes | None,
    active_user_id: str | None,
) -> GenerationProjectAuthority:
    """Select an actor-bound generation without requiring the prior scope to match."""
    if marker_json is None:
        raise RefreshRequiredError("The project has not selected generation authority.")
    if binding.mode != "cloud":
        raise GenerationControlCorruptError(
            "A local-only project cannot select hosted generation authority."
        )

    marker = _parse_marker(marker_json)
    if marker.project_id != binding.project_id:
        raise GenerationControlCorruptError("The generation marker belongs to another project.")
    if marker.store_format_version != HOSTED_STORE_FORMAT_VERSION:
        raise ClientUpgradeRequiredError(
            "This hosted store format requires another client version."
        )

    current_user_id = _active_user_id(active_user_id)
    if pointer_json is None:
        raise RefreshRequiredError("The active account has no installed generation pointer.")

    pointer = _parse_pointer(pointer_json)
    if pointer.project_id != marker.project_id or pointer.project_id != binding.project_id:
        raise GenerationControlCorruptError("The installed pointer belongs to another project.")
    if pointer.store_format_version != marker.store_format_version:
        raise GenerationControlCorruptError("The generation marker and pointer formats differ.")
    if current_user_id != pointer.installed_for_user_id:
        raise ReplicaActorMismatchError("The installed replica belongs to another account.")
    return GenerationProjectAuthority(binding=binding, marker=marker, pointer=pointer)


def select_generation_refresh_base(
    binding: ResolvedProjectBinding,
    *,
    marker_json: str | bytes | None,
    pointer_json: str | bytes | None,
    active_user_id: str | None,
) -> InstalledGenerationPointer:
    """Return an actor-bound pointer as a refresh fence, never as read authority."""
    return _select_actor_generation_authority(
        binding,
        marker_json=marker_json,
        pointer_json=pointer_json,
        active_user_id=active_user_id,
    ).pointer


def select_project_authority(
    binding: ResolvedProjectBinding,
    *,
    marker_json: str | bytes | None,
    pointer_json: str | bytes | None,
    active_user_id: str | None = None,
    active_projection_scope_id: str | None = None,
) -> ProjectAuthority:
    """Select flat or generation authority from one validated project binding."""
    if marker_json is None:
        return FlatProjectAuthority(binding)
    authority = _select_actor_generation_authority(
        binding,
        marker_json=marker_json,
        pointer_json=pointer_json,
        active_user_id=active_user_id,
    )
    current_scope_id = _active_projection_scope_id(active_projection_scope_id)
    if current_scope_id != authority.pointer.projection_scope_id:
        raise RefreshRequiredError("The installed replica requires a fresh authorization view.")
    return authority


__all__ = [
    "ClientUpgradeRequiredError",
    "FlatProjectAuthority",
    "GenerationAuthorityError",
    "GenerationAuthorityMarker",
    "GENERATION_CONTROL_SCHEMA_VERSION",
    "GenerationControlCorruptError",
    "GenerationProjectionIdentity",
    "GenerationProjectAuthority",
    "InstalledGenerationPointer",
    "ProjectAuthority",
    "RefreshRequiredError",
    "ReplicaActorMismatchError",
    "projection_identity_from_pointer",
    "select_generation_refresh_base",
    "select_project_authority",
]
