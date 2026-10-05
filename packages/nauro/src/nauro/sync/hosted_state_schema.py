"""Exact hosted state schema admitted by ordinary reference connections."""

import copy
from typing import Any

from nauro_core.mcp_tools import UPDATE_STATE


def state_schema() -> dict[str, Any]:
    schema = copy.deepcopy(UPDATE_STATE["input_schema"])
    schema.pop("required", None)
    schema["properties"].update(
        {
            "expected_revision": {
                "type": "string",
                "anyOf": [
                    {"enum": ["absent"]},
                    {"pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64},
                ],
                "description": (
                    "Required for generation state submission. Use the revision from an "
                    "authorized state read; use absent only when that read established absence. "
                    "Retry preserves the original revision."
                ),
            },
            "request_mode": {"type": "string", "enum": ["submit", "recover", "retry", "discover"]},
            "operation_id": {
                "type": "string",
                "pattern": "^state-request:[0-7][0-9A-HJKMNP-TV-Z]{25}$",
                "minLength": 40,
                "maxLength": 40,
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
    branches: list[dict[str, Any]] = []
    for mode, required, allowed in [
        (
            "submit",
            ["request_mode", "delta", "expected_revision"],
            ["project_id", "request_mode", "delta", "expected_revision"],
        ),
        (None, ["delta", "expected_revision"], ["project_id", "delta", "expected_revision"]),
        (
            "recover",
            ["request_mode", "project_id", "operation_id", "payload_digest"],
            ["project_id", "request_mode", "operation_id", "payload_digest"],
        ),
        (
            "retry",
            ["request_mode", "project_id", "operation_id", "payload_digest"],
            ["project_id", "request_mode", "operation_id", "payload_digest"],
        ),
        ("discover", ["request_mode", "project_id"], ["project_id", "request_mode", "after"]),
    ]:
        branch: dict[str, Any] = {"required": required, "propertyNames": {"enum": allowed}}
        if mode is not None:
            branch["properties"] = {"request_mode": {"const": mode}}
        branches.append(branch)
    branches.append(
        {
            "required": ["delta"],
            "not": {
                "anyOf": [
                    {"required": [name]}
                    for name in (
                        "after",
                        "expected_revision",
                        "operation_id",
                        "payload_digest",
                        "request_mode",
                    )
                ]
            },
        }
    )
    schema["oneOf"] = branches
    return schema
