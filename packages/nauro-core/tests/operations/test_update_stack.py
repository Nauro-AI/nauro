"""Kernel tests for the plan-returning ``update_stack`` operation."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nauro_core.constants import STACK_DOC_CHAR_LIMIT, STACK_REVISION_ABSENT
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes
from nauro_core.operations.update_stack import (
    UpdateStackPlan,
    compute_stack_revision,
    update_stack,
)

DOC = "# Stack\n- **Python 3.11** — primary language\n"


class TestComputeStackRevision:
    def test_lowercase_sha256_hex_of_exact_bytes(self) -> None:
        content = DOC.encode("utf-8")
        revision = compute_stack_revision(content)
        assert revision == hashlib.sha256(content).hexdigest()
        assert revision == revision.lower()
        assert len(revision) == 64

    def test_absent_token_for_missing_file(self) -> None:
        assert compute_stack_revision(None) == STACK_REVISION_ABSENT

    def test_byte_sensitivity(self) -> None:
        # The revision digests exact bytes: a trailing newline changes it.
        assert compute_stack_revision(b"# Stack") != compute_stack_revision(b"# Stack\n")


class TestUpdateStackAccepts:
    def test_returns_frozen_plan(self) -> None:
        plan = update_stack(DOC)
        assert isinstance(plan, UpdateStackPlan)
        assert plan.operation == "update_stack"
        assert plan.path == "stack.md"
        assert plan.content == DOC
        with pytest.raises(ValidationError):
            plan.content = "changed"

    def test_new_revision_is_content_digest(self) -> None:
        plan = update_stack(DOC)
        assert plan.new_revision == compute_stack_revision(DOC.encode("utf-8"))

    def test_expected_revision_defaults_to_none(self) -> None:
        assert update_stack(DOC).expected_revision is None

    def test_expected_revision_hex_accepted_and_echoed(self) -> None:
        revision = "ab" * 32
        plan = update_stack(DOC, expected_revision=revision)
        assert plan.expected_revision == revision

    def test_expected_revision_absent_token_accepted(self) -> None:
        plan = update_stack(DOC, expected_revision=STACK_REVISION_ABSENT)
        assert plan.expected_revision == STACK_REVISION_ABSENT

    def test_document_at_exact_cap_accepted(self) -> None:
        plan = update_stack("x" * STACK_DOC_CHAR_LIMIT)
        assert len(plan.content) == STACK_DOC_CHAR_LIMIT


class TestUpdateStackPayloadBytes:
    def test_canonical_json_shape(self) -> None:
        plan = update_stack(DOC, expected_revision=STACK_REVISION_ABSENT)
        assert plan.payload_bytes == canonical_payload_bytes(
            {
                "operation": "update_stack",
                "content": DOC,
                "expected_revision": STACK_REVISION_ABSENT,
            }
        )
        decoded = json.loads(plan.payload_bytes)
        assert list(decoded) == sorted(decoded)

    def test_deterministic_across_calls(self) -> None:
        assert update_stack(DOC).payload_bytes == update_stack(DOC).payload_bytes

    def test_expected_revision_participates_in_payload_identity(self) -> None:
        without = update_stack(DOC)
        with_precondition = update_stack(DOC, expected_revision="ab" * 32)
        assert without.payload_bytes != with_precondition.payload_bytes


class TestUpdateStackRejects:
    def test_nul_character_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            update_stack("# Stack\n\x00\n")
        assert "NUL" in excinfo.value.reason

    def test_over_cap_rejects_naming_the_limit(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            update_stack("x" * (STACK_DOC_CHAR_LIMIT + 1))
        assert str(STACK_DOC_CHAR_LIMIT) in excinfo.value.reason

    def test_unencodable_text_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            update_stack("# Stack\n\ud800\n")
        assert "UTF-8" in excinfo.value.reason

    def test_malformed_expected_revision_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            update_stack(DOC, expected_revision="not-a-revision")
        assert "expected_revision" in excinfo.value.reason

    def test_uppercase_expected_revision_rejects(self) -> None:
        with pytest.raises(PlanRejected):
            update_stack(DOC, expected_revision="AB" * 32)

    def test_rejection_is_typed_value_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            update_stack(DOC, expected_revision="nope")
        assert isinstance(excinfo.value, PlanRejected)
        assert excinfo.value.reason
