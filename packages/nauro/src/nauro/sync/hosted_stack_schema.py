"""Exact hosted stack schema admitted by ordinary reference connections."""

import copy
from typing import Any

from nauro_core.mcp_tools import UPDATE_STACK

REFERENCE_ID = {
    "type": "string",
    "pattern": "^stack-request:[0-7][0-9A-HJKMNP-TV-Z]{25}$",
    "minLength": 40,
    "maxLength": 40,
}


def stack_schema() -> dict[str, Any]:
    schema = copy.deepcopy(UPDATE_STACK["input_schema"])
    schema.pop("required", None)
    schema["properties"]["operation_id"]["description"] = (
        "Legacy projects: required client-generated idempotency identifier (1-128 printable "
        "ASCII characters); resending it with the same payload returns the original outcome. "
        "Selected generation projects: omit it on submission, because the server issues the "
        "identity and refuses a caller-supplied one; pass the server-issued stack-request "
        "identity only to recover or retry."
    )
    schema["properties"].update(
        {
            "expected_revision": {
                "type": "string",
                "anyOf": [
                    {"enum": ["absent"]},
                    {"pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64},
                ],
                "description": (
                    "Required for generation stack submission. Use the stack_revision from an "
                    'authorized get_raw_file("stack.md") read; use absent only when that read '
                    "found no stack.md. Retry preserves the original revision. Optional on "
                    "legacy projects, and enforced when supplied."
                ),
            },
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
        (
            "submit",
            ["request_mode", "content", "expected_revision"],
            ["project_id", "request_mode", "content", "expected_revision"],
        ),
        (None, ["content", "expected_revision"], ["project_id", "content", "expected_revision"]),
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
            "required": ["content", "operation_id"],
            "not": {
                "anyOf": [
                    {"required": [name]} for name in ("after", "payload_digest", "request_mode")
                ]
            },
        }
    )
    schema["oneOf"] = branches
    return schema
