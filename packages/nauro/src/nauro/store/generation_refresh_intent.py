from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, model_validator

from nauro.store.generation_authority import _parse_marker, _strict_json_preflight
from nauro.store.generation_refresh_state import (
    GenerationRefreshEvidenceError,
    RefreshControlPair,
    RefreshControlState,
    _carrier,
    _pointer,
)

MAX_INTENT_BYTES = 128 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class RefreshIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: StrictInt
    kind: Literal["refresh", "reconcile"]
    marker_json: StrictStr
    base_pointer_json: StrictStr
    base_authorization_json: StrictStr
    target_pointer_json: StrictStr
    target_authorization_json: StrictStr
    predecessor_digest: StrictStr | None

    @model_validator(mode="after")
    def validate_transition(self) -> RefreshIntent:
        if self.schema_version != 1:
            raise ValueError("unsupported refresh intent version")
        marker = _parse_marker(self.marker_json)
        if marker.canonical_bytes() != self.marker_json.encode():
            raise ValueError("noncanonical refresh marker")
        before = _pointer(self.base_pointer_json.encode())
        prior_view = _carrier(self.base_authorization_json.encode())
        target = RefreshControlPair(
            self.target_pointer_json.encode(), self.target_authorization_json.encode()
        )
        after = _pointer(target.pointer_json)
        for record in (before, prior_view, after):
            if (
                record.project_id != marker.project_id
                or record.store_format_version != marker.store_format_version
                or record.installed_for_user_id != before.installed_for_user_id
            ):
                raise ValueError("refresh binding mismatch")
        if after.installed_state_id in (before.installed_state_id, prior_view.installed_state_id):
            raise ValueError("refresh must allocate a new install identity")
        if self.kind == "refresh":
            RefreshControlPair(
                self.base_pointer_json.encode(), self.base_authorization_json.encode()
            )
        elif self.predecessor_digest is None:
            raise ValueError("reconciliation requires preserved evidence")
        if self.predecessor_digest is not None and (
            len(self.predecessor_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.predecessor_digest)
        ):
            raise ValueError("invalid predecessor digest")
        return self

    def classify(self, marker: bytes, pointer: bytes, carrier: bytes) -> RefreshControlState:
        _pointer(pointer)
        _carrier(carrier)
        if _parse_marker(marker).canonical_bytes() != marker:
            raise GenerationRefreshEvidenceError("Refresh marker must be canonical.")
        if marker != self.marker_json.encode():
            return "conflict"
        if pointer == self.base_pointer_json.encode():
            if carrier == self.base_authorization_json.encode():
                return "base_present"
            if carrier == self.target_authorization_json.encode():
                return "carrier_published"
        if (
            pointer == self.target_pointer_json.encode()
            and carrier == self.target_authorization_json.encode()
        ):
            return "target_present"
        return "conflict"


def encode_intent(intent: RefreshIntent) -> bytes:
    checked = RefreshIntent.model_validate(intent.model_dump())
    payload = checked.model_dump()
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    raw = _canonical({"payload": payload, "digest": digest})
    if len(raw) > MAX_INTENT_BYTES:
        raise GenerationRefreshEvidenceError("Refresh intent exceeds the size limit.")
    return raw


def decode_intent(raw: bytes) -> RefreshIntent:
    if type(raw) is not bytes or len(raw) > MAX_INTENT_BYTES:
        raise GenerationRefreshEvidenceError("Refresh intent requires bounded bytes.")
    try:
        _strict_json_preflight(raw)
        envelope = json.loads(raw)
        if type(envelope) is not dict or set(envelope) != {"payload", "digest"}:
            raise ValueError("invalid envelope")
        intent = RefreshIntent.model_validate(envelope["payload"])
        if encode_intent(intent) != raw:
            raise ValueError("noncanonical or corrupt intent")
        return intent
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise GenerationRefreshEvidenceError("Refresh intent is corrupt.") from exc


def require_predecessor(intent: RefreshIntent, raw: bytes) -> None:
    if hashlib.sha256(raw).hexdigest() != intent.predecessor_digest:
        raise GenerationRefreshEvidenceError("Refresh predecessor digest differs.")
    prior = decode_intent(raw)
    state = prior.classify(
        intent.marker_json.encode(),
        intent.base_pointer_json.encode(),
        intent.base_authorization_json.encode(),
    )
    if state == "conflict" or (intent.kind == "refresh" and state != "target_present"):
        raise GenerationRefreshEvidenceError("Refresh predecessor does not bind its successor.")
    if (
        _pointer(intent.target_pointer_json.encode()).installed_state_id
        == _pointer(prior.target_pointer_json.encode()).installed_state_id
    ):
        raise GenerationRefreshEvidenceError("Refresh successor reused its predecessor identity.")
