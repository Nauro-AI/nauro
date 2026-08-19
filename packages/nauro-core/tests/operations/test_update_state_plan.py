"""Contract tests for deterministic ``update_state`` planning."""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

import nauro_core
import nauro_core.operations as operations
from nauro_core.constants import (
    MAX_DELTA_LENGTH,
    STATE_REVISION_ABSENT,
)
from nauro_core.operations import InMemoryStore, update_state
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes
from nauro_core.operations.results import StateRevisionConflict, UpdateStateResult
from nauro_core.operations.update_state import (
    StateFileEffect,
    UpdateStatePlan,
    compute_state_revision,
    plan_state_update,
)

UPDATED_AT = datetime(2026, 8, 19, 7, 37, 59, tzinfo=timezone.utc)
CURRENT = b"# Current State\n\n- Task one\n"
HISTORY = b"## 2026-08-18T10:00Z\n\n- Earlier task\n\n---\n"
DELTA = "Task two"


def _plan(**overrides: object) -> UpdateStatePlan:
    arguments: dict[str, object] = {
        "delta": DELTA,
        "state_current_bytes": CURRENT,
        "state_history_bytes": HISTORY,
        "legacy_state_bytes": None,
        "updated_at": UPDATED_AT,
        "expected_revision": None,
    }
    arguments.update(overrides)
    return plan_state_update(**arguments)  # type: ignore[arg-type]


class TestPublicShape:
    def test_signature_is_keyword_only_and_exact(self) -> None:
        signature = inspect.signature(plan_state_update)
        assert list(signature.parameters) == [
            "delta",
            "state_current_bytes",
            "state_history_bytes",
            "legacy_state_bytes",
            "updated_at",
            "expected_revision",
        ]
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )
        assert signature.parameters["expected_revision"].default is None
        assert signature.return_annotation == "UpdateStatePlan"

    @pytest.mark.parametrize("model", [StateFileEffect, UpdateStatePlan])
    def test_models_are_frozen_closed_pydantic_models(self, model: type[BaseModel]) -> None:
        assert issubclass(model, BaseModel)
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"

    def test_effect_field_order_and_annotations(self) -> None:
        assert list(StateFileEffect.model_fields) == ["observed_bytes", "replacement_bytes"]
        assert StateFileEffect.__annotations__ == {
            "observed_bytes": "bytes | None",
            "replacement_bytes": "bytes | None",
        }

    def test_plan_field_order_and_annotations(self) -> None:
        assert list(UpdateStatePlan.model_fields) == [
            "delta",
            "expected_revision",
            "state_current_effect",
            "state_history_effect",
            "legacy_state_bytes",
            "updated_at",
            "result",
            "payload_bytes",
        ]
        assert UpdateStatePlan.__annotations__ == {
            "delta": "str",
            "expected_revision": "str | None",
            "state_current_effect": "StateFileEffect",
            "state_history_effect": "StateFileEffect",
            "legacy_state_bytes": "bytes | None",
            "updated_at": "datetime | None",
            "result": "UpdateStateResult | StateRevisionConflict",
            "payload_bytes": "bytes",
        }

    def test_derived_properties_are_not_fields_or_dump_keys(self) -> None:
        plan = _plan()
        derived = {
            "operation",
            "source",
            "state_timestamp",
            "source_revision",
            "previous_revision",
            "new_revision",
            "previous_history_revision",
            "new_history_revision",
        }
        assert derived.isdisjoint(UpdateStatePlan.model_fields)
        assert derived.isdisjoint(plan.model_dump(mode="python"))

    def test_new_names_do_not_enter_curated_exports(self) -> None:
        names = {
            "plan_state_update",
            "UpdateStatePlan",
            "StateFileEffect",
            "compute_state_revision",
        }
        assert names.isdisjoint(nauro_core.__all__)
        assert names.isdisjoint(operations.__all__)

    def test_models_are_frozen_and_reject_extra_fields(self) -> None:
        effect = StateFileEffect(observed_bytes=b"old", replacement_bytes=b"new")
        with pytest.raises(ValidationError):
            effect.observed_bytes = b"changed"
        with pytest.raises(ValidationError):
            StateFileEffect(
                observed_bytes=b"old",
                replacement_bytes=b"new",
                unexpected=True,
            )

        plan = _plan()
        with pytest.raises(ValidationError):
            plan.delta = "changed"
        with pytest.raises(ValidationError):
            UpdateStatePlan(**plan.model_dump(), unexpected=True)

    def test_plan_model_copies_bytes_and_normalizes_time(self) -> None:
        baseline = _plan()
        legacy = bytearray(b"legacy")
        payload = bytearray(b"payload")
        offset = timezone(timedelta(hours=9))
        plan = UpdateStatePlan(
            **{
                **baseline.model_dump(),
                "legacy_state_bytes": legacy,
                "updated_at": datetime(2026, 8, 19, 16, 37, tzinfo=offset),
                "payload_bytes": payload,
            }
        )

        legacy[:] = b"xxxxxx"
        payload[:] = b"xxxxxxx"

        assert plan.legacy_state_bytes == b"legacy"
        assert plan.payload_bytes == b"payload"
        assert plan.updated_at == datetime(2026, 8, 19, 7, 37, tzinfo=timezone.utc)


class TestStateFileEffect:
    def test_revisions_derive_from_exact_bytes(self) -> None:
        effect = StateFileEffect(observed_bytes=b"old\n", replacement_bytes=b"new\n")
        assert effect.observed_revision == hashlib.sha256(b"old\n").hexdigest()
        assert effect.replacement_revision == hashlib.sha256(b"new\n").hexdigest()
        assert effect.resulting_revision == effect.replacement_revision

    def test_no_replacement_means_no_write(self) -> None:
        effect = StateFileEffect(observed_bytes=b"old", replacement_bytes=None)
        assert effect.replacement_revision is None
        assert effect.resulting_revision == effect.observed_revision

    def test_absent_observation_without_replacement_stays_absent(self) -> None:
        effect = StateFileEffect(observed_bytes=None, replacement_bytes=None)
        assert effect.observed_revision == STATE_REVISION_ABSENT
        assert effect.replacement_revision is None
        assert effect.resulting_revision == STATE_REVISION_ABSENT

    def test_mutable_inputs_are_copied(self) -> None:
        observed = bytearray(b"old")
        replacement = bytearray(b"new")
        effect = StateFileEffect(
            observed_bytes=observed,
            replacement_bytes=replacement,
        )

        observed[:] = b"xxx"
        replacement[:] = b"yyy"

        assert effect.observed_bytes == b"old"
        assert effect.replacement_bytes == b"new"
        assert isinstance(effect.observed_bytes, bytes)
        assert isinstance(effect.replacement_bytes, bytes)


class TestRevisionAndPayload:
    def test_revision_is_exact_sha256_or_absent(self) -> None:
        assert compute_state_revision(None) == STATE_REVISION_ABSENT
        assert compute_state_revision(b"state") == hashlib.sha256(b"state").hexdigest()
        assert compute_state_revision(b"state") != compute_state_revision(b"state\n")

    def test_payload_identity_is_exact(self) -> None:
        plan = _plan(expected_revision="ab" * 32)
        assert plan.payload_bytes == canonical_payload_bytes(
            {
                "delta": DELTA,
                "expected_revision": "ab" * 32,
                "operation": "update_state",
            }
        )

    def test_payload_includes_null_expected_revision(self) -> None:
        plan = _plan()
        assert plan.payload_bytes == canonical_payload_bytes(
            {
                "delta": DELTA,
                "expected_revision": None,
                "operation": "update_state",
            }
        )

    def test_observations_and_time_do_not_change_payload_identity(self) -> None:
        first = _plan()
        second = _plan(
            state_current_bytes=b"different",
            state_history_bytes=None,
            legacy_state_bytes=b"ignored",
            updated_at=UPDATED_AT + timedelta(days=1),
        )
        assert first.payload_bytes == second.payload_bytes

    def test_expected_revision_changes_payload_identity(self) -> None:
        assert _plan().payload_bytes != _plan(expected_revision="ab" * 32).payload_bytes


class TestPlanningOutcomes:
    def test_planner_copies_all_observed_bytes(self) -> None:
        current = bytearray(CURRENT)
        history = bytearray(HISTORY)
        legacy = bytearray(b"ignored legacy")
        plan = _plan(
            state_current_bytes=current,
            state_history_bytes=history,
            legacy_state_bytes=legacy,
        )

        current[:] = b"x" * len(current)
        history[:] = b"x" * len(history)
        legacy[:] = b"x" * len(legacy)

        assert plan.state_current_effect.observed_bytes == CURRENT
        assert plan.state_history_effect.observed_bytes == HISTORY
        assert plan.legacy_state_bytes == b"ignored legacy"

    def test_current_source_plans_exact_final_replacements(self) -> None:
        plan = _plan()
        assert plan.operation == "update_state"
        assert plan.source == "current"
        assert plan.state_timestamp == "2026-08-19T07:37Z"
        assert plan.updated_at == UPDATED_AT
        assert plan.result == UpdateStateResult(status="ok")
        assert plan.state_current_effect.observed_bytes == CURRENT
        assert plan.state_current_effect.replacement_bytes == (
            b"# Current State\n\nTask two\n\n*Last updated: 2026-08-19T07:37Z*\n"
        )
        assert plan.state_history_effect.observed_bytes == HISTORY
        assert plan.state_history_effect.replacement_bytes == (
            HISTORY + b"## 2026-08-19T07:37Z\n\n- Task one\n\n---\n"
        )
        assert plan.previous_revision == compute_state_revision(CURRENT)
        assert plan.new_revision == compute_state_revision(
            plan.state_current_effect.replacement_bytes
        )
        assert plan.previous_history_revision == compute_state_revision(HISTORY)
        assert plan.new_history_revision == compute_state_revision(
            plan.state_history_effect.replacement_bytes
        )

    def test_offset_time_is_normalized_to_utc_before_minute_formatting(self) -> None:
        offset = timezone(timedelta(hours=9))
        plan = _plan(updated_at=datetime(2026, 8, 19, 16, 37, 59, tzinfo=offset))
        assert plan.updated_at == UPDATED_AT
        assert plan.updated_at is not None
        assert plan.updated_at.tzinfo is timezone.utc
        assert plan.state_timestamp == "2026-08-19T07:37Z"

    def test_overlap_warning_keeps_first_matching_line_and_exact_wording(self) -> None:
        current = (
            b"# Current State\n\n"
            b"- Implemented OAuth login flow with PKCE\n"
            b"- Implemented OAuth backup flow with PKCE\n"
        )
        plan = _plan(
            delta="Implemented OAuth refresh logic with PKCE",
            state_current_bytes=current,
        )
        assert plan.result == UpdateStateResult(
            status="ok",
            warning=(
                "State update shares keywords with existing entry: "
                "- Implemented OAuth login flow with PKCE"
            ),
        )

    def test_current_wins_and_malformed_unselected_legacy_is_ignored(self) -> None:
        plan = _plan(legacy_state_bytes=b"\xff")
        assert plan.source == "current"
        assert plan.legacy_state_bytes == b"\xff"
        assert plan.result.status == "ok"

    def test_legacy_source_suppresses_warning_and_is_not_deleted(self) -> None:
        legacy = b"# State\n\n- Implemented OAuth login flow with PKCE\n"
        plan = _plan(
            delta="Implemented OAuth refresh logic with PKCE",
            state_current_bytes=None,
            legacy_state_bytes=legacy,
        )
        assert plan.source == "legacy"
        assert plan.result == UpdateStateResult(status="ok")
        assert plan.legacy_state_bytes == legacy
        assert plan.state_current_effect.observed_bytes is None
        assert plan.state_current_effect.replacement_bytes == (
            b"# Current State\n\nImplemented OAuth refresh logic with PKCE\n\n"
            b"*Last updated: 2026-08-19T07:37Z*\n"
        )
        assert plan.state_history_effect.replacement_bytes == (
            HISTORY
            + b"## 2026-08-19T07:37Z\n\n# State\n\n"
            + b"- Implemented OAuth login flow with PKCE\n\n---\n"
        )

    def test_legacy_source_revision_differs_from_observed_current_revision(self) -> None:
        legacy = b"# State\n\nLegacy state\n"
        plan = _plan(state_current_bytes=None, legacy_state_bytes=legacy)
        assert plan.source_revision == compute_state_revision(legacy)
        assert plan.previous_revision == STATE_REVISION_ABSENT
        assert plan.source_revision != plan.previous_revision

    def test_empty_stripped_prior_skips_history_write_and_decode(self) -> None:
        empty_current = b"# Current State\n\n\n\n*Last updated: 2026-08-18T10:00Z*\n"
        plan = _plan(
            state_current_bytes=empty_current,
            state_history_bytes=b"\xff",
        )
        assert plan.result.status == "ok"
        assert plan.state_history_effect.observed_bytes == b"\xff"
        assert plan.state_history_effect.replacement_bytes is None
        assert plan.new_history_revision == compute_state_revision(b"\xff")

    def test_noop_retains_observations_and_payload_without_replacements(self) -> None:
        plan = _plan(
            state_current_bytes=None,
            state_history_bytes=b"\xff",
            legacy_state_bytes=None,
        )
        assert plan.source == "missing"
        assert plan.source_revision == STATE_REVISION_ABSENT
        assert plan.result == UpdateStateResult(status="noop")
        assert plan.updated_at is None
        assert plan.state_timestamp is None
        assert plan.state_current_effect == StateFileEffect(
            observed_bytes=None,
            replacement_bytes=None,
        )
        assert plan.state_history_effect == StateFileEffect(
            observed_bytes=b"\xff",
            replacement_bytes=None,
        )
        assert plan.payload_bytes == canonical_payload_bytes(
            {"delta": DELTA, "expected_revision": None, "operation": "update_state"}
        )

    def test_stale_revision_wins_over_corrupt_observations(self) -> None:
        current = b"\xff"
        plan = _plan(
            state_current_bytes=current,
            state_history_bytes=b"\xff",
            legacy_state_bytes=b"\xff",
            expected_revision=STATE_REVISION_ABSENT,
        )
        assert plan.result == StateRevisionConflict(
            expected_revision=STATE_REVISION_ABSENT,
            current_revision=compute_state_revision(current),
        )
        assert plan.source == "current"
        assert plan.updated_at is None
        assert plan.state_current_effect.replacement_bytes is None
        assert plan.state_history_effect.replacement_bytes is None

    def test_expected_revision_binds_observed_current_not_legacy_source(self) -> None:
        legacy = b"legacy"
        plan = _plan(
            state_current_bytes=None,
            legacy_state_bytes=legacy,
            expected_revision=compute_state_revision(legacy),
        )
        assert plan.result == StateRevisionConflict(
            expected_revision=compute_state_revision(legacy),
            current_revision=STATE_REVISION_ABSENT,
        )
        assert plan.source_revision == compute_state_revision(legacy)
        assert plan.previous_revision == STATE_REVISION_ABSENT


class TestValidationOrder:
    def test_delta_length_precedes_utf8_validation(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(delta="\ud800" * (MAX_DELTA_LENGTH + 1), expected_revision="bad")
        assert excinfo.value.reason == (
            f"Delta exceeds maximum length of {MAX_DELTA_LENGTH} characters "
            f"(got {MAX_DELTA_LENGTH + 1})."
        )

    def test_delta_utf8_precedes_expected_revision_validation(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(delta="\ud800", expected_revision="bad")
        assert excinfo.value.reason == "delta is not valid UTF-8-encodable text."

    def test_expected_revision_precedes_timestamp_awareness(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(expected_revision="bad", updated_at=datetime(2026, 8, 19))
        assert "expected_revision" in excinfo.value.reason

    def test_naive_timestamp_raises_exact_value_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _plan(updated_at=datetime(2026, 8, 19))
        assert str(excinfo.value) == "updated_at must be timezone-aware."

    def test_offsetless_timestamp_raises_exact_value_error(self) -> None:
        class Offsetless(timezone.__base__):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

        with pytest.raises(ValueError) as excinfo:
            _plan(updated_at=datetime(2026, 8, 19, tzinfo=Offsetless()))
        assert str(excinfo.value) == "updated_at must be timezone-aware."

    def test_selected_current_utf8_failure_propagates(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            _plan(state_current_bytes=b"\xff")

    def test_selected_legacy_utf8_failure_propagates(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            _plan(state_current_bytes=None, legacy_state_bytes=b"\xff")

    def test_required_history_utf8_failure_propagates(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            _plan(state_history_bytes=b"\xff")

    def test_empty_and_nul_delta_are_preserved(self) -> None:
        empty = _plan(delta="")
        nul = _plan(delta="a\x00b")
        assert b"\n\n\n\n*Last updated:" in empty.state_current_effect.replacement_bytes
        assert b"a\x00b" in nul.state_current_effect.replacement_bytes


class TestLocalParity:
    def test_current_path_matches_existing_store_operation_byte_for_byte(self) -> None:
        store = InMemoryStore(
            files={
                "state_current.md": CURRENT.decode(),
                "state_history.md": HISTORY.decode(),
            }
        )
        with patch("nauro_core.state._utc_timestamp", return_value="2026-08-19T07:37Z"):
            local_result = update_state(store, DELTA)

        plan = _plan()
        assert plan.result == local_result
        assert (
            plan.state_current_effect.replacement_bytes
            == store.read_file("state_current.md").encode()
        )
        assert (
            plan.state_history_effect.replacement_bytes
            == store.read_file("state_history.md").encode()
        )

    def test_planner_does_not_read_the_clock(self) -> None:
        with patch("nauro_core.state._utc_timestamp", side_effect=AssertionError("clock read")):
            plan = _plan()
        assert plan.state_timestamp == "2026-08-19T07:37Z"
