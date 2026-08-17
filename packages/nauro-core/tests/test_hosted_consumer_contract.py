"""Guard the nauro_core symbols the private hosted server imports.

The hosted MCP server is a separate private repository that imports nauro_core
submodule symbols directly, well past the curated public API. The manifest at
``hosted_consumer_symbols.txt`` lists those names; this test resolves each one
and freezes its shape against a checked-in snapshot, so a rename, a moved
module or a changed signature fails here rather than in the consumer's deploy.

Annotations are rendered here rather than by ``str()``, so the snapshot reads
the same on every Python and pydantic version. Out of scope: prose, string
contents, and ``Annotated`` metadata, whose reprs move between versions.

Regenerate the manifest with scripts/hosted_consumer_symbols.py <consumer>/src,
and the snapshot with NAURO_UPDATE_CONTRACT=1 .venv/bin/python -m pytest
packages/nauro-core/tests/test_hosted_consumer_contract.py
"""

from __future__ import annotations

import dataclasses
import difflib
import enum
import importlib
import inspect
import json
import os
import types
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

CONTRACT_VERSION = 1
TESTS_DIR = Path(__file__).parent
MANIFEST_PATH = TESTS_DIR / "hosted_consumer_symbols.txt"
SNAPSHOT_PATH = TESTS_DIR / "snapshots" / f"hosted_consumer_contract_v{CONTRACT_VERSION}.json"
_UPDATE_ENV = "NAURO_UPDATE_CONTRACT"

# Containers whose length is part of the shape; a string's contents are prose.
_SIZED_TYPES = (tuple, list, frozenset, set, dict)
# Defaults whose repr is stable; any other default is recorded as present only.
_SCALAR_TYPES = (str, bytes, int, float, bool, type(None))
_UNION_ORIGINS = (typing.Union, types.UnionType)
_MISSING = object()


class UnresolvedSymbol(LookupError):
    """A manifest name that no longer resolves to anything in nauro-core."""


def manifest_symbols() -> list[str]:
    """Return the dotted names listed in the manifest, comments and blanks dropped."""
    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _import_or_none(module_name: str) -> types.ModuleType | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _walk_attributes(obj: Any, attributes: list[str]) -> Any:
    for attribute in attributes:
        obj = getattr(obj, attribute, _MISSING)
        if obj is _MISSING:
            return _MISSING
    return obj


def resolve_symbol(dotted: str) -> object:
    """Resolve the name the way ``from parent import name`` does.

    An attribute of the longest importable prefix beats a same-named submodule.
    """
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module = _import_or_none(".".join(parts[:cut]))
        if module is None:
            continue
        obj = _walk_attributes(module, parts[cut:])
        if obj is not _MISSING:
            return obj
    module = _import_or_none(dotted)
    if module is None:
        raise UnresolvedSymbol(dotted)
    return module


def _qualified(obj: type) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _render_class_name(obj: type) -> str:
    """Render a class as ``module.qualname``, leaving the builtins module implicit."""
    return obj.__qualname__ if obj.__module__ == "builtins" else _qualified(obj)


def _render_generic(origin: Any, args: tuple[Any, ...]) -> str:
    """Render a parameterized annotation; a union sorts its members for determinism."""
    if origin in _UNION_ORIGINS:
        return " | ".join(sorted(_render_annotation(arg) for arg in args))
    if origin is typing.Literal:
        return "Literal[" + ", ".join(repr(arg) for arg in args) + "]"
    rendered = ", ".join(_render_annotation(arg) for arg in args)
    return f"{_render_annotation(origin)}[{rendered}]"


def _render_annotation(annotation: Any) -> str:
    """Render an annotation into a form that does not vary by Python or pydantic version.

    ``Annotated`` metadata is dropped: only the base type is a consumer contract.
    """
    if isinstance(annotation, str):
        return annotation
    if annotation is None or annotation is type(None):
        return "None"
    if hasattr(annotation, "__metadata__"):
        return _render_annotation(annotation.__origin__)
    origin = typing.get_origin(annotation)
    if origin is not None:
        return _render_generic(origin, typing.get_args(annotation))
    if inspect.isclass(annotation):
        return _render_class_name(annotation)
    return repr(annotation)


def _render_default(default: object) -> str:
    """Render a scalar default, standing in a placeholder for one whose repr varies."""
    return repr(default) if isinstance(default, _SCALAR_TYPES) else "<default>"


def _render_parameter(parameter: inspect.Parameter) -> str:
    """Render one parameter with its variadic marker, annotation and default."""
    prefixes = {
        inspect.Parameter.VAR_POSITIONAL: "*",
        inspect.Parameter.VAR_KEYWORD: "**",
    }
    rendered = prefixes.get(parameter.kind, "") + parameter.name
    if parameter.annotation is not inspect.Parameter.empty:
        rendered += f": {_render_annotation(parameter.annotation)}"
    if parameter.default is not inspect.Parameter.empty:
        rendered += f" = {_render_default(parameter.default)}"
    return rendered


def _kind_marker(previous: inspect.Parameter | None, current: inspect.Parameter) -> str | None:
    """Return the ``/`` or ``*`` separator that must precede the current parameter."""
    if previous is not None and previous.kind is inspect.Parameter.POSITIONAL_ONLY:
        return "/" if current.kind is not inspect.Parameter.POSITIONAL_ONLY else None
    if current.kind is not inspect.Parameter.KEYWORD_ONLY:
        return None
    barred = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.VAR_POSITIONAL)
    return None if previous is not None and previous.kind in barred else "*"


def _render_signature(obj: Any) -> str:
    """Render a callable's signature through the canonical annotation renderer."""
    signature = inspect.signature(obj)
    parameters = list(signature.parameters.values())
    tokens: list[str] = []
    for index, parameter in enumerate(parameters):
        marker = _kind_marker(parameters[index - 1] if index else None, parameter)
        tokens.extend(token for token in (marker, _render_parameter(parameter)) if token)
    if parameters and parameters[-1].kind is inspect.Parameter.POSITIONAL_ONLY:
        tokens.append("/")
    rendered = "(" + ", ".join(tokens) + ")"
    if signature.return_annotation is inspect.Signature.empty:
        return rendered
    return f"{rendered} -> {_render_annotation(signature.return_annotation)}"


def _protocol_methods(obj: type) -> list[str]:
    names = (name for name in dir(obj) if not name.startswith("_"))
    return sorted(name for name in names if callable(getattr(obj, name, None)))


def _class_detail(obj: type) -> dict[str, Any]:
    """Return the shape keys specific to the class's flavour, empty for a plain class."""
    if typing.is_typeddict(obj):
        return {"keys": sorted(getattr(obj, "__annotations__", {}))}
    if issubclass(obj, enum.Enum):
        return {"members": [member.name for member in obj]}
    if issubclass(obj, BaseModel):
        fields = {
            name: _render_annotation(field.annotation) for name, field in obj.model_fields.items()
        }
        return {"fields": fields, "frozen": bool(obj.model_config.get("frozen"))}
    if dataclasses.is_dataclass(obj):
        return {"fields": [field.name for field in dataclasses.fields(obj)]}
    if getattr(obj, "_is_protocol", False):
        return {"methods": _protocol_methods(obj)}
    return {}


def _class_shape(obj: type) -> dict[str, Any]:
    return {
        "kind": "class",
        "module": obj.__module__,
        "qualname": obj.__qualname__,
        "bases": [_qualified(base) for base in obj.__bases__],
        **_class_detail(obj),
    }


def _function_shape(obj: Any) -> dict[str, Any]:
    return {
        "kind": "function",
        "module": obj.__module__,
        "qualname": obj.__qualname__,
        "signature": _render_signature(obj),
    }


def _value_shape(obj: object) -> dict[str, Any]:
    shape: dict[str, Any] = {"kind": "value", "type": type(obj).__qualname__}
    if isinstance(obj, _SIZED_TYPES):
        shape["len"] = len(obj)
    return shape


def symbol_shape(obj: object) -> dict[str, Any]:
    """Return the JSON-serializable frozen shape of one resolved symbol."""
    if isinstance(obj, types.ModuleType):
        return {"kind": "module"}
    if inspect.isclass(obj):
        return _class_shape(obj)
    if inspect.isfunction(obj):
        return _function_shape(obj)
    return _value_shape(obj)


def build_contract() -> dict[str, Any]:
    """Assemble the consumed-symbol snapshot by resolving every manifest name."""
    return {
        "contract_version": CONTRACT_VERSION,
        "symbols": {name: symbol_shape(resolve_symbol(name)) for name in manifest_symbols()},
    }


def _dumps(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True)


def test_every_consumed_symbol_resolves() -> None:
    """Each manifest name must still exist, because the hosted server imports it."""
    missing = []
    for name in manifest_symbols():
        try:
            resolve_symbol(name)
        except UnresolvedSymbol:
            missing.append(name)
    assert not missing, (
        "These names no longer resolve in nauro-core, and the hosted mcp-server "
        "imports each one directly:\n  " + "\n  ".join(missing)
    )


def test_hosted_consumer_contract_matches_snapshot() -> None:
    current = _dumps(build_contract())

    if os.environ.get(_UPDATE_ENV) == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(current + "\n", encoding="utf-8")
        pytest.skip(f"{SNAPSHOT_PATH.name} regenerated from the live symbols")

    assert SNAPSHOT_PATH.exists(), (
        f"{SNAPSHOT_PATH} is missing. Generate it once with "
        f"{_UPDATE_ENV}=1 .venv/bin/python -m pytest {Path(__file__).name}"
    )
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8").rstrip("\n")
    if current != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                current.splitlines(),
                fromfile="snapshot (expected)",
                tofile="live symbols (current)",
                lineterm="",
                n=2,
            )
        )
        if len(diff) > 8000:
            diff = diff[:8000] + "\n... (diff truncated)"
        raise AssertionError(
            "A symbol the hosted server imports changed. This is a cross-repo change: "
            "coordinate the mcp-server update, then regenerate with "
            f"`{_UPDATE_ENV}=1 .venv/bin/python -m pytest {Path(__file__).name}` and "
            "review the diff.\n\n" + diff
        )
