"""``update_state`` — replace current state with a new delta.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`UpdateStateResult`. The kernel handles the read/migrate/write
plumbing through the :class:`~nauro_core.operations.store.Store`
protocol; snapshot capture, cloud-sync push, and length
validation stay on the adapter side.

The kernel uses the pure-function helpers in :mod:`nauro_core.state`
(``prepare_state_update`` / ``migrate_legacy_state``) so the on-disk
format is identical across surfaces.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator

from nauro_core.constants import (
    MAX_DELTA_LENGTH,
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_LEGACY_FILENAME,
    STATE_OVERLAP_MIN_KEYWORDS,
    STATE_OVERLAP_STOP_WORDS,
    STATE_REVISION_ABSENT,
)
from nauro_core.identifiers import IdentifierKind, InvalidIdentifier, validate_identifier
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes
from nauro_core.operations.results import (
    StateRevisionConflict,
    UpdateStateResult,
)
from nauro_core.operations.store import Store
from nauro_core.state import (
    _format_state_timestamp,
    _prepare_state_update_at,
    migrate_legacy_state,
    prepare_state_update,
)
from nauro_core.validation import check_content_length


def compute_state_revision(content: bytes | None) -> str:
    """Return the exact-byte SHA-256 state revision or the absent token."""
    if content is None:
        return STATE_REVISION_ABSENT
    return hashlib.sha256(content).hexdigest()


def _copy_bytes(value: object, *, field: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{field} must be bytes.")
    return memoryview(value).tobytes()


def _copy_optional_bytes(value: object, *, field: str) -> bytes | None:
    if value is None:
        return None
    return _copy_bytes(value, field=field)


def _normalize_updated_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("updated_at must be timezone-aware.")
    return value.astimezone(timezone.utc)


class StateFileEffect(BaseModel):
    """One exact observation and its optional replacement write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_bytes: bytes | None
    replacement_bytes: bytes | None

    @field_validator("observed_bytes", "replacement_bytes", mode="before")
    @classmethod
    def _copy_content(cls, value: object, info) -> bytes | None:
        return _copy_optional_bytes(value, field=info.field_name)

    @property
    def observed_revision(self) -> str:
        return compute_state_revision(self.observed_bytes)

    @property
    def replacement_revision(self) -> str | None:
        if self.replacement_bytes is None:
            return None
        return compute_state_revision(self.replacement_bytes)

    @property
    def resulting_revision(self) -> str:
        replacement_revision = self.replacement_revision
        if replacement_revision is not None:
            return replacement_revision
        return self.observed_revision


class UpdateStatePlan(BaseModel):
    """Immutable state observations, terminal outcome, and replacement effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delta: str
    expected_revision: str | None
    state_current_effect: StateFileEffect
    state_history_effect: StateFileEffect
    legacy_state_bytes: bytes | None
    updated_at: datetime | None
    result: UpdateStateResult | StateRevisionConflict
    payload_bytes: bytes

    @field_validator("legacy_state_bytes", mode="before")
    @classmethod
    def _copy_legacy_state(cls, value: object) -> bytes | None:
        return _copy_optional_bytes(value, field="legacy_state_bytes")

    @field_validator("payload_bytes", mode="before")
    @classmethod
    def _copy_payload(cls, value: object) -> bytes:
        return _copy_bytes(value, field="payload_bytes")

    @field_validator("updated_at")
    @classmethod
    def _normalize_time(cls, value: datetime | None) -> datetime | None:
        return _normalize_updated_at(value)

    @property
    def operation(self) -> Literal["update_state"]:
        return "update_state"

    @property
    def source(self) -> Literal["current", "legacy", "missing"]:
        if self.state_current_effect.observed_bytes is not None:
            return "current"
        if self.legacy_state_bytes is not None:
            return "legacy"
        return "missing"

    @property
    def state_timestamp(self) -> str | None:
        if self.updated_at is None:
            return None
        return _format_state_timestamp(self.updated_at)

    @property
    def source_revision(self) -> str:
        if self.state_current_effect.observed_bytes is not None:
            return compute_state_revision(self.state_current_effect.observed_bytes)
        return compute_state_revision(self.legacy_state_bytes)

    @property
    def previous_revision(self) -> str:
        return self.state_current_effect.observed_revision

    @property
    def new_revision(self) -> str:
        return self.state_current_effect.resulting_revision

    @property
    def previous_history_revision(self) -> str:
        return self.state_history_effect.observed_revision

    @property
    def new_history_revision(self) -> str:
        return self.state_history_effect.resulting_revision


class _StateUpdateInputs(BaseModel):
    """Copied exact observations and normalized server time for planning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_current_bytes: bytes | None
    state_history_bytes: bytes | None
    legacy_state_bytes: bytes | None
    updated_at: datetime

    @field_validator(
        "state_current_bytes",
        "state_history_bytes",
        "legacy_state_bytes",
        mode="before",
    )
    @classmethod
    def _copy_observation(cls, value: object, info) -> bytes | None:
        return _copy_optional_bytes(value, field=info.field_name)

    @field_validator("updated_at")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        normalized = _normalize_updated_at(value)
        if normalized is None:
            raise ValueError("updated_at must be timezone-aware.")
        return normalized


def plan_state_update(
    *,
    delta: str,
    state_current_bytes: bytes | None,
    state_history_bytes: bytes | None,
    legacy_state_bytes: bytes | None,
    updated_at: datetime,
    expected_revision: str | None = None,
) -> UpdateStatePlan:
    """Plan exact state replacements without I/O or clock access."""
    length_error = check_content_length(delta, "Delta", MAX_DELTA_LENGTH)
    if length_error is not None:
        raise PlanRejected(length_error)
    try:
        delta.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanRejected("delta is not valid UTF-8-encodable text.") from None

    if expected_revision is not None:
        try:
            validate_identifier(
                IdentifierKind.state_revision,
                expected_revision,
                field="expected_revision",
            )
        except InvalidIdentifier as exc:
            raise PlanRejected(str(exc)) from None

    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError("updated_at must be timezone-aware.")

    inputs = _StateUpdateInputs(
        state_current_bytes=state_current_bytes,
        state_history_bytes=state_history_bytes,
        legacy_state_bytes=legacy_state_bytes,
        updated_at=updated_at,
    )
    payload_bytes = canonical_payload_bytes(
        {
            "delta": delta,
            "expected_revision": expected_revision,
            "operation": "update_state",
        }
    )
    current_effect = StateFileEffect(
        observed_bytes=inputs.state_current_bytes,
        replacement_bytes=None,
    )
    history_effect = StateFileEffect(
        observed_bytes=inputs.state_history_bytes,
        replacement_bytes=None,
    )

    if expected_revision is not None and expected_revision != current_effect.observed_revision:
        return UpdateStatePlan(
            delta=delta,
            expected_revision=expected_revision,
            state_current_effect=current_effect,
            state_history_effect=history_effect,
            legacy_state_bytes=inputs.legacy_state_bytes,
            updated_at=None,
            result=StateRevisionConflict(
                expected_revision=expected_revision,
                current_revision=current_effect.observed_revision,
            ),
            payload_bytes=payload_bytes,
        )

    if inputs.state_current_bytes is None and inputs.legacy_state_bytes is None:
        return UpdateStatePlan(
            delta=delta,
            expected_revision=expected_revision,
            state_current_effect=current_effect,
            state_history_effect=history_effect,
            legacy_state_bytes=None,
            updated_at=None,
            result=UpdateStateResult(status="noop"),
            payload_bytes=payload_bytes,
        )

    using_legacy = inputs.state_current_bytes is None
    selected_bytes = cast(
        bytes,
        inputs.legacy_state_bytes if using_legacy else inputs.state_current_bytes,
    )
    selected_content = selected_bytes.decode("utf-8")
    if using_legacy:
        selected_content = migrate_legacy_state(selected_content).current_content

    warning = None if using_legacy else _overlap_warning(delta, selected_content)
    prepared = _prepare_state_update_at(delta, selected_content, inputs.updated_at)
    current_effect = StateFileEffect(
        observed_bytes=inputs.state_current_bytes,
        replacement_bytes=prepared.current_content.encode("utf-8"),
    )

    if prepared.history_entry is not None:
        history_content = (
            ""
            if inputs.state_history_bytes is None
            else inputs.state_history_bytes.decode("utf-8")
        )
        history_effect = StateFileEffect(
            observed_bytes=inputs.state_history_bytes,
            replacement_bytes=(history_content + prepared.history_entry).encode("utf-8"),
        )

    return UpdateStatePlan(
        delta=delta,
        expected_revision=expected_revision,
        state_current_effect=current_effect,
        state_history_effect=history_effect,
        legacy_state_bytes=inputs.legacy_state_bytes,
        updated_at=inputs.updated_at,
        result=UpdateStateResult(status="ok", warning=warning),
        payload_bytes=payload_bytes,
    )


def update_state(store: Store, delta: str) -> UpdateStateResult:
    """Replace current state with *delta*, archiving the prior body to history.
    ``status="ok"`` on a write, ``status="noop"`` when no state file exists and the
    kernel returns without writing. ``warning`` carries a keyword-overlap caution.
    """
    current_content = store.read_file(STATE_CURRENT_FILENAME)
    using_legacy = False
    if current_content is None:
        legacy_content = store.read_file(STATE_LEGACY_FILENAME)
        if legacy_content is None:
            return UpdateStateResult(status="noop")
        using_legacy = True
        migrated = migrate_legacy_state(legacy_content)
        store.write_file(STATE_CURRENT_FILENAME, migrated.current_content)
        current_content = migrated.current_content

    warning = _overlap_warning(delta, current_content) if not using_legacy else None

    result = prepare_state_update(delta, current_content)
    store.write_file(STATE_CURRENT_FILENAME, result.current_content)

    if result.history_entry is not None:
        existing_history = store.read_file(STATE_HISTORY_FILENAME) or ""
        store.write_file(STATE_HISTORY_FILENAME, existing_history + result.history_entry)

    return UpdateStateResult(status="ok", warning=warning)


def _overlap_warning(delta: str, current_content: str) -> str | None:
    """Return a caution when *delta* heavily mirrors a current-state bullet: the first
    bullet sharing :data:`STATE_OVERLAP_MIN_KEYWORDS` or more non-stop-word tokens
    with *delta*. ``None`` when no bullet qualifies.
    """
    delta_words = set(delta.lower().split())
    for line in current_content.split("\n"):
        if not line.startswith("- ") or "none yet" in line:
            continue
        line_words = set(line.lower().split())
        overlap = delta_words & line_words - STATE_OVERLAP_STOP_WORDS
        if len(overlap) >= STATE_OVERLAP_MIN_KEYWORDS:
            return f"State update shares keywords with existing entry: {line.strip()}"
    return None
