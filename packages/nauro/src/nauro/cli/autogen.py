"""Auto-generate Typer CLI commands from the ToolSpec registry.

Walks ``nauro_core.mcp_tools.ALL_TOOLS`` and, for each allowlisted tool name,
registers a command that calls the matching ``tool_<name>`` adapter in
``nauro.mcp.tools`` and prints the envelope as JSON, or renders it through the
shared read-tool renderers under ``--format text`` with a JSON fallback.

The allowlist is explicit rather than derived from ``readOnlyHint``, so a new
registry entry surfaces here only when the maintainer opts it in.

``list[str]`` properties map to repeated Typer flags; ``list[dict]`` properties
map to one JSON-valued flag parsed by ``cli._json_input``, which raises
``typer.BadParameter`` so Typer exits 2 on stderr without invoking the adapter.
Tools in ``AUTOGEN_PRIMARY_POSITIONAL`` also surface one declared optional string
property as a positional argument beside its option flag; the two merge at dispatch.
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
from nauro.cli.utils import cli_origin, resolve_target_project
from nauro.mcp import tools as mcp_tools
from nauro.mcp.rendering import resolve_renderer_kwargs, try_render_envelope
from nauro.store.repo_head import resolve_process_repo_head

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

# Optional schema properties promoted to the command's positional argument.
# The generic rule — only required properties become positionals — leaves a
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

# Properties that should never reach the CLI surface. project_id is
# replaced by ``--project NAME``; cwd is implicit from the working
# directory.
_DROPPED_PROPERTIES: frozenset[str] = frozenset({"project_id", "cwd"})


class OutputFormat(str, enum.Enum):
    """Value set of the ``--format`` flag shared by every auto-gen command."""

    json = "json"
    text = "text"


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


def _primary_option_alias(property_name: str) -> str:
    """Return the signature name for the option spelling of a primary positional.
    Only the Python parameter name is aliased; the rendered flag keeps the property's own
    name, and dispatch merges the alias back into the schema property.
    """
    return f"{property_name}_option"


def _primary_positional_params(name: str, prop: dict[str, Any]) -> list[inspect.Parameter]:
    """Build the two spellings of a primary-positional property."""
    desc = prop.get("description", "")
    positional = inspect.Parameter(
        name=name,
        kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=typer.Argument(None, help=desc),
        annotation=str,
    )
    option = inspect.Parameter(
        name=_primary_option_alias(name),
        kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=typer.Option(
            None,
            _option_flag(name),
            help=f"{desc} Equivalent to passing the value positionally.",
        ),
        annotation=str,
    )
    return [positional, option]


def _schema_to_typer_params(spec: ToolSpec) -> list[inspect.Parameter]:
    """Translate an input_schema into ordered Typer parameters.
    Required string and integer properties become positional arguments, the rest options;
    a declared primary positional surfaces as both. ``project_id`` and ``cwd`` are dropped.
    """
    schema = spec["input_schema"]
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = list(schema.get("required", []) or [])

    primary = AUTOGEN_PRIMARY_POSITIONAL.get(spec["name"])
    if primary is not None and (
        primary in required or properties.get(primary, {}).get("type") != "string"
    ):
        raise ValueError(
            f"Primary positional {primary!r} for {spec['name']!r} must be an "
            "optional string property in the tool schema. Fix "
            "AUTOGEN_PRIMARY_POSITIONAL in cli/autogen.py."
        )

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
        if name == primary:
            params.extend(_primary_positional_params(name, prop))
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

    # --format selects the output rendering; json (the default) is
    # byte-identical to the historical JSON-only surface.
    params.append(
        inspect.Parameter(
            name="output_format",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=typer.Option(
                OutputFormat.json,
                "--format",
                help=(
                    "Output format: json prints the machine-readable envelope; "
                    "text renders it for human reading."
                ),
            ),
            annotation=OutputFormat,
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
                help="Compatibility no-op; use --format to choose the output format.",
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
    """Return a ``str``-mixed Enum whose members carry the wire strings.
    Typer maps it to a native Choice, so a bad value exits 2 naming the choices; dispatch
    converts a member back to ``.value`` so the adapter sees the wire string unchanged.
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


def _exit_code_for_envelope(envelope: dict) -> int:
    """Map an envelope to its exit code, independent of output format.
    A ``rejected`` status stays exit 0: it is caller-fixable, not a transport-level error.
    """
    if envelope.get("status") == "error":
        return 1
    if envelope.get("status") == "rejected":
        return 0
    if envelope.get("error"):
        return 1
    return 0


def _error_guidance(envelope: dict) -> str:
    """Return the stderr guidance text an error envelope carries, else an empty string.
    Emitted in json mode only: text mode already carries the error prose on stdout.
    """
    if envelope.get("status") == "error":
        return envelope.get("guidance") or ""
    if envelope.get("status") == "rejected":
        return ""
    err = envelope.get("error")
    if err:
        reason = err.get("reason") if isinstance(err, dict) else str(err)
        return reason or ""
    return ""


def _emit_json_mode(envelope: dict) -> None:
    """Emit the envelope on stdout with guidance on stderr, raising on error exit codes.
    Also the text-mode fallback when no renderer applies or the renderer raised.
    """
    _emit_envelope(envelope)
    guidance = _error_guidance(envelope)
    if guidance:
        typer.echo(guidance, err=True)
    code = _exit_code_for_envelope(envelope)
    if code:
        raise typer.Exit(code=code)


def _make_command(spec: ToolSpec) -> Callable[..., None]:
    """Build the Typer callback that dispatches to the matching adapter."""
    tool_name = spec["name"]
    # Resolved at build time to fail loudly on a broken allowlist entry, and
    # again at dispatch so a patched adapter (tests) is honored.
    adapter = _resolve_adapter(tool_name)
    params = _schema_to_typer_params(spec)

    # The primary positional's option spelling is a signature-only alias:
    # it is merged into the schema property at dispatch and never reaches
    # the adapter under its alias name.
    primary = AUTOGEN_PRIMARY_POSITIONAL.get(tool_name)
    primary_alias = _primary_option_alias(primary) if primary is not None else None

    # Names of the schema-derived arguments, in dispatch order. These
    # are passed positionally to the adapter to match its
    # ``store_path, <required>, <optional>`` signature.
    schema_arg_names: list[str] = [
        p.name
        for p in params
        if p.name not in {"project", "json_output", "output_format", primary_alias}
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

    # Write adapters accept a keyword-only ``origin`` so the CLI can stamp the
    # transport; read adapters do not. The decision write adapter additionally
    # accepts ``base_commit``, stamping the HEAD of the repository enclosing
    # the CLI process cwd. Detect both once so the command stamps only the
    # surfaces that record provenance.
    adapter_params = inspect.signature(adapter).parameters
    accepts_origin = "origin" in adapter_params
    accepts_base_commit = "base_commit" in adapter_params

    def command(**kwargs: Any) -> None:
        project = kwargs.pop("project", None)
        # --json is a parity no-op; --format selects the output mode.
        kwargs.pop("json_output", None)
        output_format = kwargs.pop("output_format", OutputFormat.json)

        if primary is not None:
            option_value = kwargs.pop(primary_alias, None)
            if option_value is not None:
                if kwargs.get(primary) is not None:
                    raise typer.BadParameter(
                        f"Pass {primary.upper()} positionally or via "
                        f"{_option_flag(primary)}, not both."
                    )
                kwargs[primary] = option_value

        _project_name, store_path = resolve_target_project(project)

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
        if accepts_origin:
            adapter_kwargs["origin"] = cli_origin()
        if accepts_base_commit:
            adapter_kwargs["base_commit"] = resolve_process_repo_head()
        envelope = _resolve_adapter(tool_name)(store_path, **adapter_kwargs)

        if output_format is OutputFormat.text:
            # Same per-tool renderer-kwarg threading as the stdio server,
            # through the shared resolver so the surfaces cannot drift.
            renderer_kwargs = resolve_renderer_kwargs(tool_name, kwargs, store_path)
            # A renderer failure falls back silently to json-mode streams;
            # a traceback on stderr would break the stream contract.
            rendered = try_render_envelope(tool_name, envelope, renderer_kwargs).text
            if rendered is not None:
                # Rendered stdout is the sole carrier of error/guidance prose;
                # no stderr duplicate. Exit codes stay format-independent.
                typer.echo(rendered)
                code = _exit_code_for_envelope(envelope)
                if code:
                    raise typer.Exit(code=code)
                return
        _emit_json_mode(envelope)

    command.__signature__ = inspect.Signature(parameters=params)  # type: ignore[attr-defined]
    command.__name__ = f"autogen_{tool_name}"
    # First paragraph only: the full tool description is MCP-client prose
    # (multi-paragraph agent guidance) and would dominate --help output.
    command.__doc__ = spec["description"].split("\n\n", 1)[0]
    return command


def register_autogen_commands(app: typer.Typer) -> None:
    """Register one Typer command per allowlisted tool on ``app``.
    The allowlist is verified against ``ALL_TOOLS``, so a typo on either side fails at import.
    """
    registry_names = {spec["name"] for spec in ALL_TOOLS}
    unknown = AUTOGEN_ALLOWLIST - registry_names
    if unknown:
        raise RuntimeError(
            f"Auto-gen allowlist references unknown tools: {sorted(unknown)}. "
            "Update AUTOGEN_ALLOWLIST in cli/autogen.py."
        )
    unknown_primary = set(AUTOGEN_PRIMARY_POSITIONAL) - AUTOGEN_ALLOWLIST
    if unknown_primary:
        raise RuntimeError(
            f"Primary-positional map references non-allowlisted tools: "
            f"{sorted(unknown_primary)}. Update AUTOGEN_PRIMARY_POSITIONAL "
            "in cli/autogen.py."
        )

    for spec in ALL_TOOLS:
        if spec["name"] not in AUTOGEN_ALLOWLIST:
            continue
        command_name = _command_name(spec["name"])
        callback = _make_command(spec)
        app.command(name=command_name, help=spec["description"].split("\n\n", 1)[0])(callback)
