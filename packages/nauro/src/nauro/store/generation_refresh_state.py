from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    InstalledAuthorizationView,
    InstalledGenerationPointer,
    _parse_authorization_view,
    _parse_marker,
    _parse_pointer,
)

_MAX_CONTROL_BYTES = 16 * 1024


class GenerationRefreshEvidenceError(GenerationAuthorityError):
    code = "generation_refresh_evidence_invalid"


RefreshControlState = Literal["base_present", "carrier_published", "target_present", "conflict"]


def _bounded(raw: bytes) -> None:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_CONTROL_BYTES:
        raise GenerationRefreshEvidenceError("Refresh control evidence must be bounded bytes.")


def _pointer(raw: bytes) -> InstalledGenerationPointer:
    _bounded(raw)
    parsed = _parse_pointer(raw)
    if parsed.canonical_bytes() != raw:
        raise GenerationRefreshEvidenceError("Refresh pointer evidence must be canonical.")
    return parsed


def _carrier(raw: bytes) -> InstalledAuthorizationView:
    _bounded(raw)
    parsed = _parse_authorization_view(raw)
    if parsed.canonical_bytes() != raw:
        raise GenerationRefreshEvidenceError("Refresh authorization evidence must be canonical.")
    return parsed


@dataclass(frozen=True)
class RefreshControlPair:
    pointer_json: bytes
    authorization_json: bytes

    def __post_init__(self) -> None:
        pointer, carrier = _pointer(self.pointer_json), _carrier(self.authorization_json)
        if any(
            getattr(pointer, name) != getattr(carrier, name)
            for name in InstalledAuthorizationView.model_fields
        ):
            raise GenerationRefreshEvidenceError("Refresh control records do not form a pair.")


@dataclass(frozen=True)
class RefreshControlTransition:
    marker_json: bytes
    base: RefreshControlPair
    target: RefreshControlPair

    def __post_init__(self) -> None:
        _bounded(self.marker_json)
        marker = _parse_marker(self.marker_json)
        if marker.canonical_bytes() != self.marker_json:
            raise GenerationRefreshEvidenceError("Refresh marker evidence must be canonical.")
        if type(self.base) is not RefreshControlPair or type(self.target) is not RefreshControlPair:
            raise GenerationRefreshEvidenceError("Refresh requires validated control pairs.")
        base = RefreshControlPair(self.base.pointer_json, self.base.authorization_json)
        target = RefreshControlPair(self.target.pointer_json, self.target.authorization_json)
        before, after = _pointer(base.pointer_json), _pointer(target.pointer_json)
        if any(
            getattr(before, name) != getattr(after, name)
            for name in ("project_id", "store_format_version", "installed_for_user_id")
        ):
            raise GenerationRefreshEvidenceError("Refresh cannot cross project, format or actor.")
        if (
            marker.project_id != before.project_id
            or marker.store_format_version != before.store_format_version
        ):
            raise GenerationRefreshEvidenceError("Refresh marker does not bind the control pairs.")
        if before.installed_state_id == after.installed_state_id:
            raise GenerationRefreshEvidenceError("Refresh requires a new install-state identity.")
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "target", target)


def classify_refresh_control(
    transition: RefreshControlTransition,
    *,
    marker_json: bytes,
    pointer_json: bytes,
    authorization_json: bytes,
) -> RefreshControlState:
    """Classify bytes only; target_present proves neither permission nor durability."""
    if type(transition) is not RefreshControlTransition:
        raise GenerationRefreshEvidenceError("Refresh requires a validated transition.")
    checked = RefreshControlTransition(transition.marker_json, transition.base, transition.target)
    _bounded(marker_json)
    marker = _parse_marker(marker_json)
    if marker.canonical_bytes() != marker_json:
        raise GenerationRefreshEvidenceError("Refresh marker evidence must be canonical.")
    _pointer(pointer_json)
    _carrier(authorization_json)
    if marker_json != checked.marker_json:
        return "conflict"
    if pointer_json == checked.base.pointer_json:
        if authorization_json == checked.base.authorization_json:
            return "base_present"
        if authorization_json == checked.target.authorization_json:
            return "carrier_published"
    if (
        pointer_json == checked.target.pointer_json
        and authorization_json == checked.target.authorization_json
    ):
        return "target_present"
    return "conflict"
