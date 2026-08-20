"""Kernel tests for the hosted typed-operation outcome models.

The plan-returning operations (``update_stack``, ``share_context``,
``submit_report``) are
executed by the hosted server; these models are the cross-surface outcome
contract it returns. Pinned here because the shapes ship in nauro-core.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nauro_core.operations.results import (
    ShareContextAccepted,
    SlugConflict,
    StackRevisionConflict,
    StateRevisionConflict,
    SubmitReportAccepted,
    UpdateStackAccepted,
    WorkflowExpired,
    WorkflowInProgress,
)

REVISION = "ab" * 32
ULID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _update_stack_accepted() -> UpdateStackAccepted:
    return UpdateStackAccepted(
        stack_revision=REVISION,
        previous_revision="absent",
        receipt_id=ULID,
        event_id=ULID,
    )


def _share_context_accepted() -> ShareContextAccepted:
    return ShareContextAccepted(
        path="context/auth-cutover.md",
        question_id="Q12",
        event_id=ULID,
        content_digest=REVISION,
        receipt_id=ULID,
    )


def _submit_report_accepted(**overrides: object) -> SubmitReportAccepted:
    values: dict[str, object] = {
        "report_id": ULID,
        "event_id": ULID,
        "body_digest": REVISION,
        "receipt_id": ULID,
    }
    values.update(overrides)
    return SubmitReportAccepted(**values)


class TestUpdateStackAccepted:
    def test_dump_shape(self) -> None:
        assert _update_stack_accepted().model_dump(mode="json") == {
            "status": "ok",
            "stack_revision": REVISION,
            "previous_revision": "absent",
            "receipt_id": ULID,
            "event_id": ULID,
        }

    def test_status_is_fixed(self) -> None:
        with pytest.raises(ValidationError):
            UpdateStackAccepted(
                status="accepted",
                stack_revision=REVISION,
                previous_revision="absent",
                receipt_id=ULID,
                event_id=ULID,
            )

    def test_frozen_and_closed(self) -> None:
        result = _update_stack_accepted()
        with pytest.raises(ValidationError):
            result.stack_revision = "0" * 64
        with pytest.raises(ValidationError):
            UpdateStackAccepted(
                stack_revision=REVISION,
                previous_revision="absent",
                receipt_id=ULID,
                event_id=ULID,
                unexpected_field="value",
            )


class TestStackRevisionConflict:
    def test_dump_shape(self) -> None:
        conflict = StackRevisionConflict(
            expected_revision=REVISION,
            current_revision="absent",
        )
        assert conflict.model_dump(mode="json") == {
            "status": "stale_revision",
            "expected_revision": REVISION,
            "current_revision": "absent",
        }

    def test_frozen_and_closed(self) -> None:
        conflict = StackRevisionConflict(
            expected_revision=REVISION,
            current_revision="0" * 64,
        )
        with pytest.raises(ValidationError):
            conflict.current_revision = "absent"
        with pytest.raises(ValidationError):
            StackRevisionConflict(
                expected_revision=REVISION,
                current_revision="absent",
                unexpected_field="value",
            )


class TestStateRevisionConflict:
    def test_dump_shape(self) -> None:
        conflict = StateRevisionConflict(
            expected_revision=REVISION,
            current_revision="absent",
        )
        assert conflict.model_dump(mode="json") == {
            "status": "stale_revision",
            "expected_revision": REVISION,
            "current_revision": "absent",
        }

    def test_frozen_and_closed(self) -> None:
        conflict = StateRevisionConflict(
            expected_revision=REVISION,
            current_revision="0" * 64,
        )
        with pytest.raises(ValidationError):
            conflict.current_revision = "absent"
        with pytest.raises(ValidationError):
            StateRevisionConflict(
                expected_revision=REVISION,
                current_revision="absent",
                unexpected_field="value",
            )


class TestShareContextAccepted:
    def test_dump_shape(self) -> None:
        assert _share_context_accepted().model_dump(mode="json") == {
            "status": "ok",
            "path": "context/auth-cutover.md",
            "question_id": "Q12",
            "event_id": ULID,
            "content_digest": REVISION,
            "receipt_id": ULID,
        }

    def test_frozen_and_closed(self) -> None:
        result = _share_context_accepted()
        with pytest.raises(ValidationError):
            result.path = "context/other.md"
        with pytest.raises(ValidationError):
            ShareContextAccepted(
                path="context/x.md",
                question_id="Q1",
                event_id=ULID,
                content_digest=REVISION,
                receipt_id=ULID,
                unexpected_field="value",
            )


class TestSubmitReportAccepted:
    def test_exact_field_order_and_dump_shape(self) -> None:
        assert list(SubmitReportAccepted.model_fields) == [
            "status",
            "report_id",
            "event_id",
            "body_digest",
            "receipt_id",
        ]
        assert _submit_report_accepted().model_dump(mode="json") == {
            "status": "accepted",
            "report_id": ULID,
            "event_id": ULID,
            "body_digest": REVISION,
            "receipt_id": ULID,
        }

    def test_model_is_strict_frozen_and_closed(self) -> None:
        assert SubmitReportAccepted.model_config["strict"] is True
        result = _submit_report_accepted()
        with pytest.raises(ValidationError):
            result.report_id = "01KQ6AZGNA0B3QBF67NBXP3S46"
        with pytest.raises(ValidationError):
            _submit_report_accepted(unexpected_field="value")
        with pytest.raises(ValidationError):
            _submit_report_accepted(body_digest=123)

    def test_status_is_fixed(self) -> None:
        with pytest.raises(ValidationError):
            _submit_report_accepted(status="ok")

    @pytest.mark.parametrize("field", ["report_id", "event_id", "receipt_id"])
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "01KQ6AZGNA0B3QBF67NBXP3S4I",
            "81KQ6AZGNA0B3QBF67NBXP3S45",
            "01kq6azgna0b3qbf67nbxp3s45",
        ],
    )
    def test_all_ids_must_be_ulids(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError):
            _submit_report_accepted(**{field: value})

    @pytest.mark.parametrize(
        "body_digest",
        ["ab" * 31, "ab" * 33, "AB" * 32, "ag" * 32],
    )
    def test_body_digest_must_be_lowercase_sha256(self, body_digest: str) -> None:
        with pytest.raises(ValidationError):
            _submit_report_accepted(body_digest=body_digest)

    def test_original_report_id_equals_event_id(self) -> None:
        with pytest.raises(ValidationError):
            _submit_report_accepted(event_id="01KQ6AZGNA0B3QBF67NBXP3S46")


class TestSlugConflict:
    def test_dump_shape(self) -> None:
        conflict = SlugConflict(slug="auth-cutover", suggested_slug="auth-cutover-2")
        assert conflict.model_dump(mode="json") == {
            "status": "slug_conflict",
            "slug": "auth-cutover",
            "suggested_slug": "auth-cutover-2",
        }

    def test_frozen_and_closed(self) -> None:
        conflict = SlugConflict(slug="a", suggested_slug="a-2")
        with pytest.raises(ValidationError):
            conflict.slug = "b"
        with pytest.raises(ValidationError):
            SlugConflict(slug="a", suggested_slug="a-2", unexpected_field="value")


class TestWorkflowOutcomes:
    def test_expired_dump_shape(self) -> None:
        assert WorkflowExpired(operation_id="op-1").model_dump(mode="json") == {
            "status": "expired",
            "operation_id": "op-1",
        }

    def test_in_progress_dump_shape(self) -> None:
        assert WorkflowInProgress(operation_id="op-1").model_dump(mode="json") == {
            "status": "in_progress",
            "operation_id": "op-1",
        }

    @pytest.mark.parametrize("model", [WorkflowExpired, WorkflowInProgress])
    def test_frozen_and_closed(self, model) -> None:
        result = model(operation_id="op-1")
        with pytest.raises(ValidationError):
            result.operation_id = "op-2"
        with pytest.raises(ValidationError):
            model(operation_id="op-1", unexpected_field="value")

    @pytest.mark.parametrize("model", [WorkflowExpired, WorkflowInProgress])
    def test_operation_id_is_required(self, model) -> None:
        with pytest.raises(ValidationError):
            model()
