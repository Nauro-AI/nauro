"""Auto-generate Typer CLI commands from the ToolSpec registry.

Walks ``nauro_core.mcp_tools.ALL_TOOLS`` and, for each tool name in the
allowlist, registers a Typer command that calls the matching
``tool_<name>`` adapter in ``nauro.mcp.tools`` and prints the resulting
envelope as JSON. Auto-generation keeps the CLI surface in lockstep
with the MCP surface.

The allowlist is explicit (not derived from the ``readOnlyHint``
annotation) so future additions to the registry — read or write —
surface through this generator only when the maintainer opts them in.

``list[str]`` properties map to repeated Typer flags (Typer aggregates
them natively). ``list[dict]`` properties map to a single JSON-valued
flag parsed at dispatch time by ``cli._json_input``; that helper raises
``typer.BadParameter`` on malformed input so Typer renders to stderr at
exit 2 without ever invoking the adapter.
"""

from __future__ import annotations

import enum
import inspect
import json
from collections.abc import Callable
from typing import Any

import typer
from nauro_core.mcp_tools import ALL_TOOLS, ToolSpec

from nauro.cli._json_input import parse_json_list_of_dicts
from nauro.cli.utils import resolve_target_project
from nauro.mcp import tools as mcp_tools
from nauro.telemetry.transport import set_transport

# Tools that should auto-generate a CLI command. list_projects is
# excluded — local installs auto-resolve to a single project and do not
# need a discovery entry point. Write tools opt in explicitly so future
# additions to ALL_TOOLS cannot reach the CLI through this generator by
# accident. Any tool name added here must have a matching ``tool_<name>``
# adapter in ``nauro.mcp.tools``.
AUTOGEN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "get_context",
        "get_raw_file",
        "list_decisions",
        "get_decision",
        "diff_since_last_session",
        "search_decisions",
        "check_decision",
        "update_state",
        "flag_question",
        "propose_decision",
    }
)

# Properties that should never reach the CLI surface. project_id is
# replaced by ``--project NAME``; cwd is implicit from the working
# directory.
_DROPPED_PROPERTIES: frozenset[str] = frozenset({"project_id", "cwd"})


def _command_name(tool_name: str) -> str:
    """Convert a snake_case tool name to a kebab-case CLI command name."""
    return tool_name.replace("_", "-")


def _option_flag(property_name: str) -> str:
    """Build the long-form Typer option flag for a schema property."""
    return "--" + property_name.replace("_", "-")


def _bool_option_flag(property_name: str) -> str:
    """Build the ``--flag/--no-flag`` form for a boolean option."""
    kebab = property_name.replace("_", "-")
    return f"--{kebab}/--no-{kebab}"


def _schema_to_typer_params(spec: ToolSpec) -> list[inspect.Parameter]:
    """Translate an input_schema into ordered Typer parameters.

    Required string/integer properties become positional arguments;
    everything else becomes an option. project_id and cwd are dropped.
    """
    schema = spec["input_schema"]
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = list(schema.get("required", []) or [])

    params: list[inspect.Parameter] = []

    # Required first, in the order they appear in `required`.
    for name in required:
        if name in _DROPPED_PROPERTIES or name not in properties:
            continue
        prop = properties[name]
        annotation, default = _required_param(name, prop)
        params.append(
            inspect.Parameter(
                name=name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )

    # Then options, in property-declaration order.
    for name, prop in properties.items():
        if name in _DROPPED_PROPERTIES or name in required:
            continue
        annotation, default = _optional_param(name, prop)
        params.append(
            inspect.Parameter(
                name=name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )

    # --project flag, common to every auto-gen command.
    params.append(
        inspect.Parameter(
            name="project",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=typer.Option(
                None,
                "--project",
                help="Project name (default: resolve from cwd).",
            ),
            annotation=str,
        )
    )

    # --json no-op preserved for parity with hand-written commands.
    params.append(
        inspect.Parameter(
            name="json_output",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=typer.Option(
                True,
                "--json/--no-json",
                help="Emit JSON output (default; no-op identity for parity).",
            ),
            annotation=bool,
        )
    )

    return params


def _required_param(name: str, prop: dict[str, Any]) -> tuple[Any, Any]:
    """Return (annotation, default) for a required schema property."""
    prop_type = prop.get("type")
    desc = prop.get("description", "")
    if prop_type == "string":
        return (str, typer.Argument(..., help=desc))
    if prop_type == "integer":
        return (int, typer.Argument(..., help=desc))
    raise ValueError(
        f"Unsupported required property type {prop_type!r} for {name!r}. "
        "Extend the type-coercion table in cli/autogen.py."
    )


# Generated enum types are cached per (property, values) so repeated walks
# of ALL_TOOLS reuse one type rather than minting fresh ones. Identity churn
# is otherwise harmless here (commands build once at import) but the cache is
# cheap insurance.
_ENUM_CACHE: dict[tuple[str, tuple[str, ...]], type[enum.Enum]] = {}


def _enum_for_property(name: str, enum_values: list[str]) -> type[enum.Enum]:
    """Build a ``str``-mixed Enum whose members carry the wire strings.

    Used as the Typer annotation for an enum-valued property so Typer maps it
    to a native Choice. A bad value then exits 2 with a choices-naming message
    across typer/click versions, where ``click.Choice`` exits 1 without one
    under typer>=0.26 / click>=8.4. Each member's ``.value`` is its wire
    string, so the valid value reaches the adapter byte-identically once the
    dispatch converts the member back to its ``.value``.
    """
    key = (name, tuple(enum_values))
    cached = _ENUM_CACHE.get(key)
    if cached is not None:
        return cached
    class_name = name.replace("_", " ").title().replace(" ", "") + "Choice"
    generated = enum.Enum(class_name, {value: value for value in enum_values}, type=str)
    _ENUM_CACHE[key] = generated
    return generated


def _optional_param(name: str, prop: dict[str, Any]) -> tuple[Any, Any]:
    """Return (annotation, default) for an optional schema property."""
    prop_type = prop.get("type")
    desc = prop.get("description", "")
    default_value = prop.get("default")

    enum_values = prop.get("enum")

    if prop_type == "string" and enum_values:
        enum_type = _enum_for_property(name, list(enum_values))
        default_member = enum_type(default_value) if default_value is not None else None
        return (
            enum_type,
            typer.Option(
                default_member,
                _option_flag(name),
                help=desc,
            ),
        )
    if prop_type == "string":
        return (str, typer.Option(default_value, _option_flag(name), help=desc))
    if prop_type == "integer":
        return (int, typer.Option(default_value, _option_flag(name), help=desc))
    if prop_type == "boolean":
        return (
            bool,
            typer.Option(
                bool(default_value) if default_value is not None else False,
                _bool_option_flag(name),
                help=desc,
            ),
        )
    if prop_type == "array":
        items_type = (prop.get("items") or {}).get("type")
        if items_type == "string":
            return (
                list[str],
                typer.Option(
                    None,
                    _option_flag(name),
                    help=f"{desc} Repeat the flag for each value.",
                ),
            )
        if items_type == "object":
            return (
                str | None,
                typer.Option(
                    None,
                    _option_flag(name),
                    help=(
                        f"{desc} Pass JSON: literal '[{{...}}]', '@file.json', or '-' for stdin."
                    ),
                    metavar="JSON",
                ),
            )
    raise ValueError(
        f"Unsupported optional property type {prop_type!r} for {name!r}. "
        "Extend the type-coercion table in cli/autogen.py."
    )


def _resolve_adapter(tool_name: str) -> Callable[..., dict]:
    """Look up the ``tool_<name>`` adapter in ``nauro.mcp.tools``."""
    attr = f"tool_{tool_name}"
    adapter = getattr(mcp_tools, attr, None)
    if adapter is None:
        raise AttributeError(
            f"Auto-gen allowlist references {tool_name!r} but nauro.mcp.tools has no {attr}."
        )
    return adapter


def _emit_envelope(envelope: dict) -> None:
    """Pretty-print the envelope to stdout."""
    typer.echo(json.dumps(envelope, indent=2))


def _exit_for_envelope(envelope: dict) -> None:
    """If the envelope signals an error, print guidance to stderr and exit 1.

    Caller-fixable rejections (``status: "rejected"``) carry a structured
    ``error`` payload but stay exit 0 — they are not transport-level errors
    and the envelope on stdout already carries the reason.
    """
    if envelope.get("status") == "error":
        guidance = envelope.get("guidance") or ""
        if guidance:
            typer.echo(guidance, err=True)
        raise typer.Exit(code=1)
    if envelope.get("status") == "rejected":
        return
    if envelope.get("error"):
        err = envelope["error"]
        reason = err.get("reason") if isinstance(err, dict) else str(err)
        if reason:
            typer.echo(reason, err=True)
        raise typer.Exit(code=1)


def _make_command(spec: ToolSpec) -> Callable[..., None]:
    """Build the Typer callback that dispatches to the matching adapter."""
    tool_name = spec["name"]
    adapter = _resolve_adapter(tool_name)
    params = _schema_to_typer_params(spec)

    # Names of the schema-derived arguments, in dispatch order. These
    # are passed positionally to the adapter to match its
    # ``store_path, <required>, <optional>`` signature.
    schema_arg_names: list[str] = [
        p.name for p in params if p.name not in {"project", "json_output"}
    ]

    # Properties declared as ``array of object`` arrive as raw strings from
    # Typer and must be JSON-parsed before reaching the adapter. Precompute
    # the names once at command-construction time.
    props: dict[str, Any] = spec["input_schema"].get("properties", {}) or {}
    json_array_names: list[str] = [
        name
        for name in schema_arg_names
        if (
            props.get(name, {}).get("type") == "array"
            and (props.get(name, {}).get("items") or {}).get("type") == "object"
        )
    ]

    # Enum-valued properties arrive as Enum members from Typer. The adapters
    # and kernel expect the plain wire string (str()/f-string formatting and
    # ``DecisionConfidence(value)`` paths would otherwise see a member), so
    # convert each set member back to its ``.value`` before dispatch.
    enum_arg_names: list[str] = [
        name for name in schema_arg_names if props.get(name, {}).get("enum")
    ]

    def command(**kwargs: Any) -> None:
        project = kwargs.pop("project", None)
        # --json is a parity no-op; JSON is the only output mode.
        kwargs.pop("json_output", None)

        _project_name, store_path = resolve_target_project(project)

        set_transport("cli")

        for name in json_array_names:
            raw = kwargs.get(name)
            if raw is None:
                continue
            kwargs[name] = parse_json_list_of_dicts(raw, _option_flag(name))

        for name in enum_arg_names:
            value = kwargs.get(name)
            if isinstance(value, enum.Enum):
                kwargs[name] = value.value

        adapter_kwargs = {name: kwargs[name] for name in schema_arg_names if name in kwargs}
        envelope = adapter(store_path, **adapter_kwargs)
        _emit_envelope(envelope)
        _exit_for_envelope(envelope)

    command.__signature__ = inspect.Signature(parameters=params)  # type: ignore[attr-defined]
    command.__name__ = f"autogen_{tool_name}"
    command.__doc__ = spec["description"]
    return command


def register_autogen_commands(app: typer.Typer) -> None:
    """Register one Typer command per allowlisted tool on ``app``.

    The allowlist is verified against ``ALL_TOOLS`` so a typo in either
    surface fails loudly at import time.
    """
    registry_names = {spec["name"] for spec in ALL_TOOLS}
    unknown = AUTOGEN_ALLOWLIST - registry_names
    if unknown:
        raise RuntimeError(
            f"Auto-gen allowlist references unknown tools: {sorted(unknown)}. "
            "Update AUTOGEN_ALLOWLIST in cli/autogen.py."
        )

    for spec in ALL_TOOLS:
        if spec["name"] not in AUTOGEN_ALLOWLIST:
            continue
        command_name = _command_name(spec["name"])
        callback = _make_command(spec)
        app.command(name=command_name, help=spec["description"])(callback)
