"""Exact hosted share schema admitted by ordinary reference connections."""

import copy
from typing import Any

from nauro_core.mcp_tools import SHARE_CONTEXT

REFERENCE_ID = {
    "type": "string",
    "pattern": "^share-request:[0-7][0-9A-HJKMNP-TV-Z]{25}$",
    "minLength": 40,
    "maxLength": 40,
}


def share_schema() -> dict[str, Any]:
    schema = copy.deepcopy(SHARE_CONTEXT["input_schema"])
    content = schema.pop("required")
    schema["properties"]["operation_id"]["description"] = (
        "Legacy projects: required client-generated idempotency identifier (1-128 printable "
        "ASCII characters); resending it with the same payload returns the original outcome. "
        "Selected generation projects: omit it on submission, because the server issues the "
        "identity and refuses a caller-supplied one; pass the server-issued share-request "
        "identity only to recover or retry."
    )
    schema["properties"].update(
        {
            "request_mode": {"type": "string", "enum": ["submit", "recover", "retry", "discover"]},
            "payload_digest": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "minLength": 64,
                "maxLength": 64,
            },
            "after": {"type": "string", "maxLength": 256},
        }
    )
    reference = ["project_id", "request_mode", "operation_id", "payload_digest"]
    branches: list[dict[str, Any]] = []
    for mode, required, allowed in [
        ("submit", ["request_mode", *content], ["project_id", "request_mode", *content]),
        (None, [*content], ["project_id", *content]),
        ("recover", ["request_mode", "project_id", "operation_id", "payload_digest"], reference),
        ("retry", ["request_mode", "project_id", "operation_id", "payload_digest"], reference),
        ("discover", ["request_mode", "project_id"], ["project_id", "request_mode", "after"]),
    ]:
        branch: dict[str, Any] = {"required": required, "propertyNames": {"enum": allowed}}
        if mode is not None:
            branch["properties"] = {"request_mode": {"const": mode}}
        if mode in ("recover", "retry"):
            branch["properties"]["operation_id"] = dict(REFERENCE_ID)
        branches.append(branch)
    branches.append(
        {
            "required": [*content, "operation_id"],
            "not": {
                "anyOf": [
                    {"required": [name]} for name in ("after", "payload_digest", "request_mode")
                ]
            },
        }
    )
    schema["oneOf"] = branches
    return schema
