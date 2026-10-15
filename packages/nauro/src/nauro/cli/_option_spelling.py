"""CLI spelling of schema properties shared by the generated and generation-write commands."""

from __future__ import annotations

from typing import Any

import typer

# Optional schema properties promoted to the command's positional argument.
# The generic rule (only required properties become positionals) leaves a
# tool whose primary payload is optional in the schema without a positional
# form: flag_question's ``question`` may be omitted (the resolve action
# passes ``resolved_by`` instead), yet ``nauro flag-question "..."`` is the
# invocation the flag action naturally suggests. A tool named here surfaces
# the designated string property both as an optional positional argument and
# as its regular option flag; dispatch merges the two spellings and rejects
# the call when both carry a value. The MCP schema is unchanged.
AUTOGEN_PRIMARY_POSITIONAL: dict[str, str] = {
    "flag_question": "question",
}


def option_flag(property_name: str) -> str:
    """Build the long-form Typer option flag for a schema property."""
    return "--" + property_name.replace("_", "-")


def primary_option_alias(property_name: str) -> str:
    """Return the signature name for the option spelling of a primary positional.
    Only the Python parameter name is aliased; the rendered flag keeps the property's own
    name, and dispatch merges the alias back into the schema property.
    """
    return f"{property_name}_option"


def merge_primary_positional(tool_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``kwargs`` with a primary positional's option spelling merged into the property.
    Raises ``typer.BadParameter`` when both spellings carry a value.
    """
    primary = AUTOGEN_PRIMARY_POSITIONAL.get(tool_name)
    if primary is None:
        return kwargs
    merged = dict(kwargs)
    option_value = merged.pop(primary_option_alias(primary), None)
    if option_value is not None:
        if merged.get(primary) is not None:
            raise typer.BadParameter(
                f"Pass {primary.upper()} positionally or via {option_flag(primary)}, not both."
            )
        merged[primary] = option_value
    return merged
