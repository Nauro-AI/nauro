"""Exact hosted question schema admitted by ordinary reference connections."""

import copy
from typing import Any

from nauro_core.mcp_tools import FLAG_QUESTION

CONTENT = {"question", "context", "targets", "resolved_by"}
REFERENCES = {"operation_id", "payload_digest", "after"}


def question_schema() -> dict[str, Any]:
    schema = copy.deepcopy(FLAG_QUESTION["input_schema"])
    schema.pop("required", None)
    schema["properties"].update(
        {
            "request_mode": {"type": "string", "enum": ["submit", "recover", "retry", "discover"]},
            "operation_id": {
                "type": "string",
                "pattern": "^question-request:[0-9A-HJKMNP-TV-Z]{26}$",
                "minLength": 43,
                "maxLength": 43,
            },
            "payload_digest": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "minLength": 64,
                "maxLength": 64,
            },
            "after": {"type": "string", "maxLength": 256},
        }
    )
    schema["additionalProperties"] = False
    branches: list[dict[str, Any]] = []
    for mode, required, forbidden in [
        ("submit", ["request_mode"], REFERENCES),
        (
            "recover",
            ["request_mode", "project_id", "operation_id", "payload_digest"],
            CONTENT | {"after"},
        ),
        (
            "retry",
            ["request_mode", "project_id", "operation_id", "payload_digest"],
            CONTENT | {"after"},
        ),
        ("discover", ["request_mode", "project_id"], CONTENT | {"operation_id", "payload_digest"}),
    ]:
        branches.append(
            {
                "properties": {"request_mode": {"const": mode}},
                "required": required,
                "not": {"anyOf": [{"required": [name]} for name in sorted(forbidden)]},
            }
        )
    branches[0]["oneOf"] = [
        {"required": ["question"], "not": {"required": ["resolved_by"]}},
        {
            "required": ["resolved_by", "targets"],
            "not": {"anyOf": [{"required": ["question"]}, {"required": ["context"]}]},
        },
    ]
    branches.append(
        {"not": {"anyOf": [{"required": [name]} for name in sorted(REFERENCES | {"request_mode"})]}}
    )
    schema["oneOf"] = branches
    return schema
