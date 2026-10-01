"""Typed generation writes through the standard CLI commands."""

from __future__ import annotations

import enum
import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from nauro_core.mcp_tools import ToolSpec

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.migration_admission import migration_write_guard, require_migration_admission
from nauro.sync.generation_writes import CONTENT, FAMILIES, generation_write
from nauro.templates.generation_guidance import regenerate_refreshed_guidance


class WriteMode(str, enum.Enum):
    submit = "submit"
    discover = "discover"
    recover = "recover"
    retry = "retry"


def _options(family: str) -> dict[str, type]:
    options = {"request_mode": WriteMode, "operation_id": str, "payload_digest": str}
    if family == "state":
        options["expected_revision"] = str
    return options


def _parameters(command: Callable[..., None], family: str) -> list[inspect.Parameter]:
    parameters = []
    options = _options(family)
    for parameter in inspect.signature(command).parameters.values():
        if parameter.name in options:
            continue
        adapted = parameter
        if parameter.name in CONTENT[family] and isinstance(
            parameter.default, typer.models.ArgumentInfo
        ):
            adapted = parameter.replace(default=typer.Argument(None))
        parameters.append(adapted)
    help_text = {
        "request_mode": (
            "Submit (default), local discover, lookup-only recover, "
            "or absent-only retry of the saved attempt."
        ),
        "operation_id": "Original saved operation identity for recover or retry.",
        "payload_digest": "Original saved payload digest for recover or retry.",
        "expected_revision": "Optional revision from an authorized state read.",
    }
    parameters.extend(
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=annotation,
            default=typer.Option(None, "--" + name.replace("_", "-"), help=help_text[name]),
        )
        for name, annotation in options.items()
    )
    return parameters


def _request(family: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    request = {"project_id": kwargs.get("project")}
    for name in CONTENT[family] | _options(family).keys():
        value = kwargs.get(name)
        if isinstance(value, enum.Enum):
            value = value.value
        if value is not None and value != []:
            request[name] = value
    if family == "question" and kwargs.get("question_option") is not None:
        if request.get("question") is not None:
            raise typer.BadParameter("Pass question once.")
        request["question"] = kwargs["question_option"]
    return request


def with_write_options(command: Callable[..., None], spec: ToolSpec) -> Callable[..., None]:
    family = FAMILIES[spec["name"]]

    def dispatch(**kwargs: Any) -> None:
        try:
            result = generation_write(
                spec["name"], _request(family, kwargs), on_refreshed=regenerate_refreshed_guidance
            )
        except (GenerationAuthorityError, OSError) as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(1) from None
        except (ValueError, TypeError) as error:
            raise typer.BadParameter(str(error)) from None
        if result is None:
            if family in {"stack", "share"}:
                raise typer.BadParameter("This command requires a generation replica.")
            for name in spec["input_schema"].get("required", []):
                if kwargs.get(name) is None:
                    raise typer.BadParameter(f"Missing argument {name.upper()}")
            for name in _options(family):
                kwargs.pop(name, None)
            command(**kwargs)
            return
        typer.echo(json.dumps(result, indent=2))
        if result.get("unresolved") or result.get("status") not in {"committed", "discovered"}:
            raise typer.Exit(1)

    dispatch.__signature__ = inspect.Signature(parameters=_parameters(command, family))  # type: ignore[attr-defined]
    dispatch.__name__ = command.__name__
    dispatch.__doc__ = command.__doc__
    return dispatch


def require_legacy_write(store_path: Path, command: str) -> None:
    try:
        require_migration_admission(store_path)
    except PermissionError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    # Incomplete replica evidence must not reopen the legacy writer.
    try:
        (store_path / ".replica").lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        typer.echo("Error: Cannot verify project write authority.", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Error: nauro {command} is unavailable for generation replicas.", err=True)
    raise typer.Exit(1)


@contextmanager
def legacy_write_guard(store_path: Path, command: str) -> Iterator[None]:
    require_legacy_write(store_path, command)
    with migration_write_guard(store_path):
        require_legacy_write(store_path, command)
        yield


def register_extra_writes(app: typer.Typer) -> None:
    from nauro_core.mcp_tools import SHARE_CONTEXT, UPDATE_STACK

    from nauro.cli.autogen import _schema_to_typer_params

    for spec in (UPDATE_STACK, SHARE_CONTEXT):

        def unavailable(**kwargs: Any) -> None:
            raise typer.BadParameter("This command requires a generation replica.")

        unavailable.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters=_schema_to_typer_params(spec)
        )
        callback = with_write_options(unavailable, spec)
        app.command(name=spec["name"].replace("_", "-"), help=spec["description"])(callback)
