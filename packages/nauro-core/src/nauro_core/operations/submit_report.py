"""Deterministic planning for original report submission."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes

MAX_REPORT_BODY_BYTES = 51_200


class SubmitReportPlan(BaseModel):
    """Frozen identity and exact body bytes for an original report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["submit_report"] = "submit_report"
    body: str
    body_bytes: bytes
    body_digest: str
    payload_bytes: bytes

    @model_validator(mode="after")
    def _validate_derivations(self) -> SubmitReportPlan:
        try:
            body_bytes = self.body.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("body is not valid UTF-8-encodable text.") from None
        if self.body_bytes != body_bytes:
            raise ValueError("body_bytes must equal the exact UTF-8 encoding of body.")
        if self.body_digest != hashlib.sha256(body_bytes).hexdigest():
            raise ValueError("body_digest must equal the SHA-256 digest of body_bytes.")
        if self.payload_bytes != canonical_payload_bytes(
            {"body": self.body, "operation": "submit_report"}
        ):
            raise ValueError("payload_bytes must equal the canonical submit_report payload.")
        return self


def submit_report(body: str) -> SubmitReportPlan:
    """Validate an exact original report body into a frozen execution plan."""
    if not isinstance(body, str):
        raise TypeError("body must be str.")
    if body == "":
        raise PlanRejected("body is empty.")
    if "\x00" in body:
        raise PlanRejected("body contains a NUL character.")
    try:
        body_bytes = body.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanRejected("body is not valid UTF-8-encodable text.") from None
    if len(body_bytes) > MAX_REPORT_BODY_BYTES:
        raise PlanRejected(
            f"body exceeds the {MAX_REPORT_BODY_BYTES}-byte report cap "
            f"(got {len(body_bytes)} bytes)."
        )

    return SubmitReportPlan(
        body=body,
        body_bytes=body_bytes,
        body_digest=hashlib.sha256(body_bytes).hexdigest(),
        payload_bytes=canonical_payload_bytes({"body": body, "operation": "submit_report"}),
    )
