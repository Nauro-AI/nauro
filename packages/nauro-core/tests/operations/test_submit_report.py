"""Contract tests for deterministic original report submission planning."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

import nauro_core
import nauro_core.operations as operations
import nauro_core.operations.submit_report as submit_report_module
from nauro_core.mcp_tools import ALL_TOOLS, HOSTED_TOOLS
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes
from nauro_core.operations.submit_report import (
    MAX_REPORT_BODY_BYTES,
    SubmitReportPlan,
    submit_report,
)

BODY = "Café\nReady ✅"


def _plan(body: str = BODY) -> SubmitReportPlan:
    return submit_report(body)


class TestPublicShape:
    def test_signature_is_exact(self) -> None:
        signature = inspect.signature(submit_report)
        assert list(signature.parameters) == ["body"]
        assert signature.parameters["body"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert signature.parameters["body"].annotation == "str"
        assert signature.return_annotation == "SubmitReportPlan"

    def test_report_body_cap_is_exact(self) -> None:
        assert MAX_REPORT_BODY_BYTES == 51_200

    def test_plan_is_frozen_closed_pydantic_model(self) -> None:
        assert issubclass(SubmitReportPlan, BaseModel)
        assert SubmitReportPlan.model_config["frozen"] is True
        assert SubmitReportPlan.model_config["extra"] == "forbid"

        plan = _plan()
        with pytest.raises(ValidationError):
            plan.body = "changed"
        with pytest.raises(ValidationError):
            SubmitReportPlan(**plan.model_dump(), unexpected=True)

    def test_plan_field_order_and_annotations(self) -> None:
        assert list(SubmitReportPlan.model_fields) == [
            "operation",
            "body",
            "body_bytes",
            "body_digest",
            "payload_bytes",
        ]
        assert SubmitReportPlan.model_fields["operation"].annotation == Literal["submit_report"]
        assert SubmitReportPlan.model_fields["body"].annotation is str
        assert SubmitReportPlan.model_fields["body_bytes"].annotation is bytes
        assert SubmitReportPlan.model_fields["body_digest"].annotation is str
        assert SubmitReportPlan.model_fields["payload_bytes"].annotation is bytes

    def test_new_names_do_not_enter_curated_exports(self) -> None:
        names = {"submit_report", "SubmitReportPlan", "SubmitReportAccepted"}
        assert names.isdisjoint(nauro_core.__all__)
        assert names.isdisjoint(operations.__all__)

    def test_no_request_or_rejection_models_exist(self) -> None:
        assert not hasattr(submit_report_module, "SubmitReportRequest")
        assert not hasattr(submit_report_module, "SubmitReportRejected")
        assert not hasattr(submit_report_module, "ReportRejectionCode")

    def test_tool_registries_do_not_include_submit_report(self) -> None:
        assert "submit_report" not in {spec["name"] for spec in ALL_TOOLS}
        assert "submit_report" not in {spec["name"] for spec in HOSTED_TOOLS}


class TestAcceptedPlan:
    def test_exact_body_derivations(self) -> None:
        plan = _plan()
        body_bytes = BODY.encode("utf-8")
        assert plan.operation == "submit_report"
        assert plan.body == BODY
        assert plan.body_bytes == body_bytes
        assert plan.body_digest == hashlib.sha256(body_bytes).hexdigest()
        assert plan.payload_bytes == canonical_payload_bytes(
            {"body": BODY, "operation": "submit_report"}
        )

    def test_canonical_unicode_example_is_exact(self) -> None:
        plan = _plan()
        assert plan.body_bytes == b"Caf\xc3\xa9\nReady \xe2\x9c\x85"
        assert plan.body_digest == (
            "59fdb52f865222408638e32c80d4308fa01f69b4ced44f375978e616c9d6c55c"
        )
        assert plan.payload_bytes == (
            b'{"body":"Caf\xc3\xa9\\nReady \xe2\x9c\x85","operation":"submit_report"}'
        )
        assert hashlib.sha256(plan.payload_bytes).hexdigest() == (
            "47114a619d4ffe921ae9e18222fa6055103e02d545e8913fe9e54771c948cc9a"
        )

    def test_exact_ascii_byte_cap_is_accepted(self) -> None:
        plan = _plan("x" * MAX_REPORT_BODY_BYTES)
        assert len(plan.body_bytes) == MAX_REPORT_BODY_BYTES

    def test_exact_four_byte_code_point_cap_is_accepted(self) -> None:
        plan = _plan("😀" * 12_800)
        assert len(plan.body_bytes) == MAX_REPORT_BODY_BYTES

    def test_whitespace_only_body_is_preserved(self) -> None:
        plan = _plan(" \t\r\n")
        assert plan.body == " \t\r\n"
        assert plan.body_bytes == b" \t\r\n"


class TestRejection:
    @pytest.mark.parametrize(
        "body, reason",
        [
            ("", "body is empty."),
            ("body\x00", "body contains a NUL character."),
            ("body\ud800", "body is not valid UTF-8-encodable text."),
            (
                "x" * (MAX_REPORT_BODY_BYTES + 1),
                "body exceeds the 51200-byte report cap (got 51201 bytes).",
            ),
        ],
    )
    def test_exact_reason(self, body: str, reason: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(body)
        assert excinfo.value.reason == reason

    def test_nul_precedes_utf8_encoding(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan("\x00\ud800")
        assert excinfo.value.reason == "body contains a NUL character."

    def test_utf8_encoding_precedes_byte_limit(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan("\ud800" + "x" * (MAX_REPORT_BODY_BYTES + 1))
        assert excinfo.value.reason == "body is not valid UTF-8-encodable text."

    def test_four_byte_code_point_over_cap_names_encoded_size(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan("😀" * 12_801)
        assert excinfo.value.reason == (
            "body exceeds the 51200-byte report cap (got 51204 bytes)."
        )

    def test_non_string_is_programming_error(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            submit_report(b"body")  # type: ignore[arg-type]
        assert not isinstance(excinfo.value, PlanRejected)


class TestIdentity:
    def test_equal_bodies_produce_equal_plan_identity(self) -> None:
        first = _plan()
        second = _plan()
        assert first.body_bytes == second.body_bytes
        assert first.body_digest == second.body_digest
        assert first.payload_bytes == second.payload_bytes

    @pytest.mark.parametrize(
        "first, second",
        [
            ("body", "body\n"),
            ("body ", "body"),
            ("line\r\n", "line\n"),
            ("Café", "Cafe\u0301"),
        ],
    )
    def test_any_body_byte_change_changes_identity(self, first: str, second: str) -> None:
        first_plan = _plan(first)
        second_plan = _plan(second)
        assert first_plan.body_bytes != second_plan.body_bytes
        assert first_plan.body_digest != second_plan.body_digest
        assert first_plan.payload_bytes != second_plan.payload_bytes

    def test_payload_contains_only_operation_and_body(self) -> None:
        plan = _plan()
        assert json.loads(plan.payload_bytes) == {
            "body": BODY,
            "operation": "submit_report",
        }
        assert set(json.loads(plan.payload_bytes)) == {"body", "operation"}


class TestDirectConstruction:
    @pytest.mark.parametrize(
        "field, replacement",
        [
            ("body_bytes", b"different"),
            ("body_digest", "0" * 64),
            ("payload_bytes", b"{}"),
        ],
    )
    def test_rejects_divergent_derived_fact(self, field: str, replacement: object) -> None:
        values = _plan().model_dump()
        values[field] = replacement
        with pytest.raises(ValidationError):
            SubmitReportPlan(**values)

    def test_rejects_non_submit_report_operation(self) -> None:
        values = _plan().model_dump()
        values["operation"] = "update_stack"
        with pytest.raises(ValidationError):
            SubmitReportPlan(**values)
