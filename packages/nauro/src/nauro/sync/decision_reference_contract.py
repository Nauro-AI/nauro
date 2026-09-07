"""Saved decision requests and reference-only MCP contract verification."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from typing import Any

from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.mcp_tools import PROPOSE_DECISION
from nauro_core.operations.commit_plan import (
    HostedPreTeamApprovalPayloadV1,
    canonical_judgment_payload_bytes,
)
from nauro_core.provenance import validate_utc_timestamp

from nauro.store.submission_records import SubmissionScope
from nauro.sync.judgment_transport import _unique_object, verify_judgment_response

MAX_RESPONSE = 5 * 1024 * 1024
CONTENT = {
    "rationale",
    "title",
    "operation",
    "affected_decision_id",
    "rejected",
    "confidence",
    "decision_type",
    "reversibility",
    "files_affected",
    "resolves_questions",
}


class DecisionReferenceError(ValueError):
    """No verified result is available. Discover or recover; do not resend automatically."""


def _json(raw: str | bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )


def _reference(value: object) -> None:
    if not isinstance(value, str) or not value.startswith("decision-request:"):
        raise ValueError("A prepared server reference is required")
    validate_identifier(
        IdentifierKind.ulid, value.removeprefix("decision-request:"), field="reference"
    )


def validate_arguments(arguments: dict[str, Any], project: str) -> str:
    if arguments.get("project_id") != project:
        raise DecisionReferenceError("The request does not select the configured project")
    mode = arguments.get("request_mode", "prepare")
    if not isinstance(mode, str):
        raise DecisionReferenceError("Request mode must be a string")
    common = {"project_id", "request_mode"}
    if mode == "prepare":
        allowed = common | CONTENT
        if not isinstance(arguments.get("rationale"), str):
            raise DecisionReferenceError("Preparation requires rationale")
    elif mode == "discover":
        allowed = common | {"after"}
        if "after" in arguments and not isinstance(arguments["after"], str):
            raise DecisionReferenceError("Invalid discovery cursor")
    elif mode in {"submit", "recover", "retry"}:
        allowed = common | {"operation_id", "payload_digest"}
        _reference(arguments.get("operation_id"))
        digest = arguments.get("payload_digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise DecisionReferenceError("The saved payload digest is required")
    else:
        raise DecisionReferenceError("Unknown request mode")
    if set(arguments) - allowed:
        raise DecisionReferenceError("Replacement content or unsupported request fields")
    return mode


def _verify_saved_request(value: dict[str, Any], project: str, actor: str) -> None:
    saved = value["request"]
    if set(saved) != {
        "project_id",
        "actor_id",
        "operation_id",
        "created_at",
        "payload_digest",
        "payload_json",
    }:
        raise ValueError("Invalid saved request")
    if saved["project_id"] != project or saved["actor_id"] != actor:
        raise ValueError("Saved request scope mismatch")
    _reference(saved["operation_id"])
    validate_utc_timestamp(saved["created_at"], field="created_at")
    raw = saved["payload_json"].encode()
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != saved["payload_digest"]:
        raise ValueError("Saved bytes differ from digest")
    _json(raw)
    payload = HostedPreTeamApprovalPayloadV1.model_validate_json(raw, strict=True)
    draft = payload.model_dump(mode="json")
    if canonical_judgment_payload_bytes(draft) != raw or value["effective_draft"] != draft:
        raise ValueError("Effective draft differs from canonical request")


def verify_observation(value: dict[str, Any], project: str, actor: str) -> None:
    from datetime import datetime

    expected = {
        "version",
        "request",
        "effective_draft",
        "admission",
        "admitted_at",
        "deadline",
        "status",
        "unresolved",
        "execution",
    }
    if set(value) != expected or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Invalid observation envelope")
    _verify_saved_request(value, project, actor)
    saved = value["request"]
    status = value["status"]
    if type(value["unresolved"]) is not bool or value["unresolved"] != (
        status in {"unresolved", "pending"}
    ):
        raise ValueError("Invalid unresolved status")
    if value["admission"] == "never_admitted":
        if status not in {"prepared", "stale"} or any(
            value[k] is not None for k in ("execution", "admitted_at", "deadline")
        ):
            raise ValueError("Never-admitted request has execution evidence")
        return
    if value["admission"] != "admitted":
        raise ValueError("Invalid admission")
    for field in ("admitted_at", "deadline"):
        validate_utc_timestamp(value[field], field=field)
    admitted = datetime.fromisoformat(value["admitted_at"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(value["deadline"].replace("Z", "+00:00"))
    if deadline - admitted != timedelta(hours=24):
        raise ValueError("Invalid original deadline")
    scope = SubmissionScope(
        project_id=project,
        user_id=actor,
        operation_kind="judgment_commit",
        operation_id=saved["operation_id"],
    )
    result = verify_judgment_response(
        json.dumps(value["execution"]).encode(), scope, saved["payload_digest"]
    )
    observed = "unresolved" if result.status == "absent" else result.status
    if status != observed and not (status == "stale" and result.status == "absent"):
        raise ValueError("Execution and observation disagree")


def reference_schema() -> dict[str, Any]:
    schema = copy.deepcopy(PROPOSE_DECISION["input_schema"])
    schema["properties"].update(
        {
            "request_mode": {
                "type": "string",
                "enum": ["prepare", "submit", "recover", "retry", "discover"],
                "default": "prepare",
            },
            "operation_id": {
                "type": "string",
                "description": "Server-issued saved request reference.",
            },
            "payload_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "after": {
                "type": "string",
                "description": "Opaque discovery cursor; one exact request per page.",
            },
        }
    )
    schema["required"] = ["project_id"]
    schema["additionalProperties"] = False
    schema["oneOf"] = [
        {
            "properties": {"request_mode": {"enum": ["prepare"]}},
            "required": ["rationale"],
            "not": {
                "anyOf": [
                    {"required": [name]} for name in ("operation_id", "payload_digest", "after")
                ]
            },
        },
        {
            "properties": {"request_mode": {"enum": ["submit", "recover", "retry"]}},
            "required": ["request_mode", "operation_id", "payload_digest"],
            "not": {"anyOf": [{"required": [name]} for name in sorted(CONTENT | {"after"})]},
        },
        {
            "properties": {"request_mode": {"enum": ["discover"]}},
            "required": ["request_mode"],
            "not": {
                "anyOf": [
                    {"required": [name]}
                    for name in sorted(CONTENT | {"operation_id", "payload_digest"})
                ]
            },
        },
    ]
    return schema
