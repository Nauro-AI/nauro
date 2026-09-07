"""Explicit prepared-reference delivery for the existing decision command."""

from __future__ import annotations

import enum
import inspect
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import typer
from nauro_core.mcp_tools import ToolSpec
from pydantic import BaseModel, ConfigDict

from nauro.auth import ActiveCredentials
from nauro.cli._json_input import parse_json_list_of_dicts
from nauro.sync.decision_reference import DecisionReferenceError, DecisionReferenceTransport
from nauro.sync.decision_reference_contract import _json, validate_arguments


class ReferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    endpoint: str
    project_id: str
    actor_id: str
    credentials_file: str


class ReferenceCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    access_token: str


class RequestMode(str, enum.Enum):
    prepare = "prepare"
    submit = "submit"
    discover = "discover"
    recover = "recover"
    retry = "retry"


def _private_json(path: Path) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Use an owner-only regular file")
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("File exceeds size limit")
        return _json(raw)


def _credentials(path: Path) -> ActiveCredentials:
    record = ReferenceCredentials.model_validate(_private_json(path))
    if not record.user_id or not record.access_token:
        raise ValueError("Missing credentials")
    return ActiveCredentials(record.user_id, record.access_token)


def reference_client() -> httpx.Client:
    return httpx.Client()


def _execute(profile: ReferenceProfile, request: dict[str, Any]) -> dict[str, Any]:
    with reference_client() as client:
        transport = DecisionReferenceTransport(
            profile.endpoint,
            profile.project_id,
            profile.actor_id,
            client,
            lambda: _credentials(Path(profile.credentials_file)),
        )
        return transport.propose_decision(**request)


def _reference_call(
    path: Path, kwargs: dict[str, Any], spec: ToolSpec, context: typer.Context
) -> None:
    if kwargs["project"] is not None or kwargs["output_format"].value != "json":
        raise typer.BadParameter("Reference delivery uses the profile project and JSON output")
    try:
        profile = ReferenceProfile.model_validate(_private_json(path))
        if not Path(profile.credentials_file).is_absolute():
            raise ValueError("Credentials path must be absolute")
    except (ValueError, OSError):
        raise typer.BadParameter("Invalid reference profile; use an owner-only file") from None
    request: dict[str, Any] = {"project_id": profile.project_id}
    names = set(spec["input_schema"]["properties"]) - {"project_id", "cwd"}
    names.update({"request_mode", "operation_id", "payload_digest", "after"})
    for name in names:
        value = kwargs.get(name)
        source = context.get_parameter_source(name)
        if source is None or source.name == "DEFAULT" or value is None:
            continue
        if isinstance(value, enum.Enum):
            value = value.value
        if name == "rejected":
            value = parse_json_list_of_dicts(value, "--rejected")
        request[name] = value
    try:
        validate_arguments(request, profile.project_id)
    except ValueError:
        raise typer.BadParameter(
            "Invalid reference arguments for the selected request mode"
        ) from None
    try:
        result = _execute(profile, request)
    except (DecisionReferenceError, ValueError, OSError):
        typer.echo(
            "No verified result. Use discover or recover with this profile. No retry was sent.",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, indent=2))


def with_reference_options(command: Callable[..., None], spec: ToolSpec) -> Callable[..., None]:
    signature = inspect.signature(command)
    params = [
        param.replace(default=typer.Argument(None, help="Decision rationale."))
        if param.name == "rationale"
        else param
        for param in signature.parameters.values()
    ]
    params.append(
        inspect.Parameter(
            "context",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=typer.Context,
            default=None,
        )
    )
    options = [
        ("reference_profile", Path, "Explicit operator profile for prepared-reference delivery."),
        (
            "request_mode",
            RequestMode,
            "Reference mode; defaults to prepare. Approve its effective draft before submit.",
        ),
        ("operation_id", str, "Saved request reference, not a newly generated identity."),
        ("payload_digest", str, "Digest returned with the saved request."),
        ("after", str, "Discovery cursor returned by the server."),
    ]
    params.extend(
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=annotation,
            default=typer.Option(None, "--" + name.replace("_", "-"), help=help_text),
        )
        for name, annotation, help_text in options
    )

    def dispatch(**kwargs: Any) -> None:
        context = kwargs.pop("context")
        profile = kwargs.pop("reference_profile")
        if profile is not None:
            _reference_call(profile, kwargs, spec, context)
            return
        reference_values = [kwargs.pop(name) for name, _, _ in options[1:]]
        if any(value is not None for value in reference_values):
            raise typer.BadParameter("Reference modes require --reference-profile")
        if kwargs.get("rationale") is None:
            raise typer.BadParameter("Missing argument RATIONALE")
        command(**kwargs)

    dispatch.__signature__ = inspect.Signature(parameters=params)  # type: ignore[attr-defined]
    dispatch.__name__ = command.__name__
    dispatch.__doc__ = command.__doc__
    return dispatch
