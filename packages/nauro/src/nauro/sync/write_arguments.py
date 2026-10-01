"""Mode-specific requirements for typed write requests."""

from __future__ import annotations

from typing import Any

from nauro_core.mcp_tools import FLAG_QUESTION, SHARE_CONTEXT, UPDATE_STACK, UPDATE_STATE

WRITE_SPECS = {
    "update_state": UPDATE_STATE,
    "flag_question": FLAG_QUESTION,
    "update_stack": UPDATE_STACK,
    "share_context": SHARE_CONTEXT,
}
REFERENCE_FIELDS = {"operation_id", "payload_digest"}
CONTENT_FIELDS = {
    name: set(spec["input_schema"]["properties"]) - REFERENCE_FIELDS - {"project_id"}
    for name, spec in WRITE_SPECS.items()
}
CONTENT_FIELDS["update_state"].add("expected_revision")
MODE_DESCRIPTIONS = {
    "request_mode": (
        "Submit (default), discover local saved attempts, recover by lookup only, "
        "or retry the original attempt only after an absent lookup."
    ),
    "operation_id": "Original saved operation identity. Required for recover and retry.",
    "payload_digest": "Original saved payload digest. Required for recover and retry.",
}


def validate_write_arguments(operation: str, arguments: dict[str, Any]) -> None:
    mode = arguments.get("request_mode")
    if mode is None:
        mode = "submit"
    if not isinstance(mode, str) or mode not in {"submit", "discover", "recover", "retry"}:
        raise ValueError("Invalid write request mode.")
    supplied = {key for key, value in arguments.items() if value is not None}
    if mode != "submit" and supplied & CONTENT_FIELDS[operation]:
        raise ValueError("Reference modes cannot replace content.")
    if mode in {"submit", "discover"} and supplied & REFERENCE_FIELDS:
        raise ValueError("This mode cannot take a saved reference.")
    if mode in {"recover", "retry"}:
        _require_strings(arguments, sorted(REFERENCE_FIELDS), mode)
    if mode == "submit":
        required = list(WRITE_SPECS[operation]["input_schema"].get("required", []))
        if operation == "flag_question":
            required = _question_required(arguments)
        _require_strings(arguments, required, "Submit")


def _require_strings(arguments: dict[str, Any], names: list[str], mode: str) -> None:
    for name in names:
        if name not in arguments or not isinstance(arguments[name], str):
            raise ValueError(f"{mode} requires {name}.")


def _question_required(arguments: dict[str, Any]) -> list[str]:
    question = arguments.get("question") is not None
    resolution = arguments.get("resolved_by") is not None
    if question == resolution:
        raise ValueError("Submit requires exactly one of question or resolved_by.")
    if resolution and arguments.get("context") is not None:
        raise ValueError("Resolution cannot replace question content.")
    return ["question" if question else "resolved_by"]


def write_mode_schema(operation: str) -> dict[str, Any]:
    submit: dict[str, Any] = {
        "properties": {
            "request_mode": {"enum": ["submit", None]},
            **{name: {"type": "null"} for name in REFERENCE_FIELDS},
        },
    }
    required = list(WRITE_SPECS[operation]["input_schema"].get("required", []))
    submit["required"] = required
    submit["properties"].update({name: {"type": "string"} for name in required})
    if operation == "flag_question":
        submit["oneOf"] = [
            {
                "required": ["question"],
                "properties": {
                    "question": {"type": "string"},
                    "resolved_by": {"type": "null"},
                },
            },
            {
                "required": ["resolved_by"],
                "properties": {
                    "resolved_by": {"type": "string"},
                    "question": {"type": "null"},
                    "context": {"type": "null"},
                },
            },
        ]
    reference_properties = {name: {"type": "null"} for name in CONTENT_FIELDS[operation]}
    discover = {
        "required": ["request_mode"],
        "properties": {
            "request_mode": {"const": "discover"},
            **reference_properties,
            **{name: {"type": "null"} for name in REFERENCE_FIELDS},
        },
    }
    recover = {
        "required": ["request_mode", "operation_id", "payload_digest"],
        "properties": {
            "request_mode": {"enum": ["recover", "retry"]},
            **reference_properties,
            **{name: {"type": "string"} for name in REFERENCE_FIELDS},
        },
    }
    return {"oneOf": [submit, discover, recover]}
