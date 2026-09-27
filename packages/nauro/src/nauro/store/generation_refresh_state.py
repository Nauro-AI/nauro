from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nauro.store.generation_authority import (
    GenerationAuthorityError,
    InstalledAuthorizationView,
    InstalledGenerationPointer,
    _parse_authorization_view,
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
