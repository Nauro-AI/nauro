"""Kernel tests for the hosted typed-operation outcome models.

The plan-returning operations (``update_stack``, ``share_context``) are
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
