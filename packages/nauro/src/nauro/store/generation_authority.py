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

_CONTROL_SCHEMA_VERSION = 1
_STRICT_MODEL_CONFIG = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_control_bytes(model: pyd.BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


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
        if value != _CONTROL_SCHEMA_VERSION:
            raise ValueError("unsupported generation control schema")
        return value

    def canonical_bytes(self) -> bytes:
        return _canonical_control_bytes(self)


class InstalledGenerationPointer(pyd.BaseModel):
    """The actor-bound pointer for one verified installed generation."""

    model_config = _STRICT_MODEL_CONFIG

    schema_version: pyd.StrictInt
    project_id: pyd.StrictStr
    store_format_version: pyd.StrictInt = pyd.Field(ge=1)
    generation_id: pyd.StrictStr
    manifest_digest: pyd.StrictStr
    committed_at: pyd.StrictStr
    installed_at: pyd.StrictStr
    installed_state_id: pyd.StrictStr
    installed_for_user_id: pyd.StrictStr
    projection_class: Literal["viewer", "contributor_plus"]
    projection_scope_id: pyd.StrictStr

    _validate_ulids = pyd.field_validator(
        "project_id",
        "generation_id",
        "installed_state_id",
        "installed_for_user_id",
    )(_ulid)
    _validate_digests = pyd.field_validator(
        "manifest_digest",
        "projection_scope_id",
    )(_sha256)
    _validate_timestamps = pyd.field_validator("committed_at", "installed_at")(_timestamp)

    @pyd.field_validator("schema_version")
    @classmethod
    def _supported_schema(cls, value: int) -> int:
        if value != _CONTROL_SCHEMA_VERSION:
            raise ValueError("unsupported generation control schema")
        return value

    def canonical_bytes(self) -> bytes:
        return _canonical_control_bytes(self)


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


@dataclass(frozen=True)
class _PendingGenerationAuthority:
    binding: ResolvedProjectBinding
    marker: GenerationAuthorityMarker
    active_user_id: str


def _strict_json_preflight(raw: str | bytes) -> None:
    text = raw.decode("utf-8") if type(raw) is bytes else raw
    assert type(text) is str
    if text.startswith("\ufeff"):
        raise ValueError("JSON byte order marks are not allowed")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON constant")

    json.loads(text, object_pairs_hook=object_from_pairs, parse_constant=reject_constant)


def _parse_marker(raw: str | bytes) -> GenerationAuthorityMarker:
    if type(raw) not in (str, bytes):
        raise GenerationControlCorruptError("The generation authority marker is invalid.")
    try:
        _strict_json_preflight(raw)
        return GenerationAuthorityMarker.model_validate_json(raw)
    except (pyd.ValidationError, ValueError, TypeError, RecursionError) as exc:
        raise GenerationControlCorruptError("The generation authority marker is invalid.") from exc


def _parse_pointer(raw: str | bytes) -> InstalledGenerationPointer:
    if type(raw) not in (str, bytes):
        raise GenerationControlCorruptError("The installed generation pointer is invalid.")
    try:
        _strict_json_preflight(raw)
        return InstalledGenerationPointer.model_validate_json(raw)
    except (pyd.ValidationError, ValueError, TypeError, RecursionError) as exc:
        raise GenerationControlCorruptError("The installed generation pointer is invalid.") from exc


def _active_user_id(value: str | None) -> str:
    if type(value) is not str:
        raise ReplicaActorMismatchError("The installed replica does not match an active account.")
    try:
        return validate_identifier(IdentifierKind.ulid, value, field="active_user_id")
    except ValueError as exc:
        raise ReplicaActorMismatchError(
            "The installed replica does not match an active account."
        ) from exc


def _active_projection_scope_id(value: str | None) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise RefreshRequiredError("The installed replica requires a fresh authorization view.")
    return value


def _select_marker_authority(
    binding: ResolvedProjectBinding,
    *,
    marker_json: str | bytes | None,
    active_user_id: str | None,
) -> FlatProjectAuthority | _PendingGenerationAuthority:
    if marker_json is None:
        return FlatProjectAuthority(binding)
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
    return _PendingGenerationAuthority(
        binding=binding,
        marker=marker,
        active_user_id=_active_user_id(active_user_id),
    )


def _select_generation_pointer(
    pending: _PendingGenerationAuthority,
    *,
    pointer_json: str | bytes | None,
    active_projection_scope_id: str | None,
) -> GenerationProjectAuthority:
    if pointer_json is None:
        raise RefreshRequiredError("The active account has no installed generation pointer.")

    pointer = _parse_pointer(pointer_json)
    marker = pending.marker
    binding = pending.binding
    if pointer.project_id != marker.project_id or pointer.project_id != binding.project_id:
        raise GenerationControlCorruptError("The installed pointer belongs to another project.")
    if pointer.store_format_version != marker.store_format_version:
        raise GenerationControlCorruptError("The generation marker and pointer formats differ.")
    if pending.active_user_id != pointer.installed_for_user_id:
        raise ReplicaActorMismatchError("The installed replica belongs to another account.")

    current_scope_id = _active_projection_scope_id(active_projection_scope_id)
    if current_scope_id != pointer.projection_scope_id:
        raise RefreshRequiredError("The installed replica requires a fresh authorization view.")
    return GenerationProjectAuthority(binding=binding, marker=marker, pointer=pointer)


def select_project_authority(
    binding: ResolvedProjectBinding,
    *,
    marker_json: str | bytes | None,
    pointer_json: str | bytes | None,
    active_user_id: str | None = None,
    active_projection_scope_id: str | None = None,
) -> ProjectAuthority:
    """Select flat or generation authority from one validated project binding."""
    selected = _select_marker_authority(
        binding,
        marker_json=marker_json,
        active_user_id=active_user_id,
    )
    if isinstance(selected, FlatProjectAuthority):
        return selected
    return _select_generation_pointer(
        selected,
        pointer_json=pointer_json,
        active_projection_scope_id=active_projection_scope_id,
    )


__all__ = [
    "ClientUpgradeRequiredError",
    "FlatProjectAuthority",
    "GenerationAuthorityError",
    "GenerationAuthorityMarker",
    "GenerationControlCorruptError",
    "GenerationProjectAuthority",
    "InstalledGenerationPointer",
    "ProjectAuthority",
    "RefreshRequiredError",
    "ReplicaActorMismatchError",
    "select_project_authority",
]
