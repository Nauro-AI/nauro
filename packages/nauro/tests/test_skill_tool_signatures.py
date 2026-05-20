"""Structural drift tests for MCP tool-call signatures in skill surfaces.

Skill bodies, ``MCP_INSTRUCTIONS_STATIC``, and the chat adopt-prompt doc
contain prose examples of MCP tool calls of the form
``tool_name(arg, kwarg=value)``. When the canonical schemas in
``nauro_core.mcp_tools.ALL_TOOLS`` drift (renamed param, removed tool,
new required field), those baked-in examples become wrong and the agent
copies the broken signature into real MCP calls.

This module locates ``name(...)`` patterns in the surfaces and validates
each one against ``ALL_TOOLS``:

- balanced parens (unmatched ``(`` fails loudly with line number)
- no phantom params (kwarg keys + positional placeholders must name real
  schema params)
- required params present in calls that look structurally complete
- no misspelled tool names (Nauro-prefixed identifiers like
  ``propose_decison(...)`` that aren't in ``ALL_TOOLS``)

The phrase-level retired-string guards live in ``test_skills_drift.py``.
This file is the structural companion — it understands tool calls as
data, not as substrings.

## Parser scope

The parser is a deliberate naive walker, not a Python parser. It works
because surfaces follow a narrow convention: positional args are
parameter-name placeholders (``check_decision(proposed_approach)``),
kwargs are ``name=opaque_value``. Values are ignored except
``operation="..."`` so the per-operation required-set exception for
``propose_decision`` can be applied.

The misspelled-tool guard uses a prefix allowlist derived from
``ALL_TOOLS`` (``propose_``, ``check_``, …) because a wide-open
identifier scan picks up parenthetical prose like ``(rule in manifest)``
as false positives. Typos that mangle the prefix itself
(``propsoe_decision``) still slip past — accepted gap.

No regex engine is used; identifier extraction and balanced-paren
matching are explicit character walks.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

import pytest
from nauro_core.mcp_tools import ALL_TOOLS, ToolSpec

from tests._skill_surfaces import SKILL_SURFACES

_TOOL_BY_NAME: dict[str, ToolSpec] = {spec["name"]: spec for spec in ALL_TOOLS}
_TOOL_NAMES: tuple[str, ...] = tuple(_TOOL_BY_NAME)

# Underscore-leading slice of each tool name — drives the misspelled-tool
# guard's scope.
_TOOL_PREFIXES: tuple[str, ...] = tuple(
    sorted({name.split("_", 1)[0] + "_" for name in _TOOL_NAMES})
)

_SURFACE_PARAMS = list(SKILL_SURFACES.items())


class ToolCall(NamedTuple):
    """Parsed ``name(...)`` occurrence. ``args is None`` means unbalanced parens."""

    name: str
    args: str | None
    offset: int


# ── Character-walk primitives ───────────────────────────────────────────────


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _at(surface: str, text: str, offset: int) -> str:
    """``surface:line`` prefix for error messages."""
    return f"{surface}:{_line_number(text, offset)}"


def _iter_identifiers(text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(identifier, offset)`` for each non-digit-led word-char run."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _is_word_char(ch) and not ch.isdigit():
            start = i
            i += 1
            while i < n and _is_word_char(text[i]):
                i += 1
            yield text[start:i], start
        else:
            i += 1


def _skip_inline_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _find_balanced_close(text: str, open_idx: int) -> int | None:
    """Return the index of ``)`` matching ``(`` at ``open_idx``, or None."""
    depth = 0
    quote: str | None = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# ── Arg-string parser ───────────────────────────────────────────────────────


def _split_top_level_commas(args_str: str) -> list[str]:
    """Split on commas not inside (), [], {}, or quoted strings."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in args_str:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_args(args_str: str) -> tuple[list[str], dict[str, str]]:
    """Return ``(positional_names, kwargs)``.

    Positional entries that are not bare identifiers are dropped — by
    convention positional args in skill bodies stand in for the schema
    parameter of the same name.
    """
    if not args_str.strip():
        return [], {}
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    for raw in _split_top_level_commas(args_str):
        part = raw.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            if key.isidentifier():
                kwargs[key] = value.strip()
        elif part.isidentifier():
            positional.append(part)
    return positional, kwargs


# ── Call discovery ──────────────────────────────────────────────────────────


def find_tool_calls(text: str) -> list[ToolCall]:
    """Return a ``ToolCall`` for every ``name(...)`` in ``text``.

    Unbalanced calls are emitted with ``args=None`` so the caller can
    surface them as a hard failure with line number.
    """
    results: list[ToolCall] = []
    for ident, start in _iter_identifiers(text):
        if ident not in _TOOL_BY_NAME:
            continue
        after = _skip_inline_whitespace(text, start + len(ident))
        if after >= len(text) or text[after] != "(":
            continue
        close = _find_balanced_close(text, after)
        args = None if close is None else text[after + 1 : close]
        results.append(ToolCall(name=ident, args=args, offset=after))
    return results


def _iter_well_formed_calls(text: str) -> Iterator[ToolCall]:
    """``find_tool_calls`` minus the unbalanced ones (surfaced separately)."""
    for call in find_tool_calls(text):
        if call.args is not None:
            yield call


def _find_misspelled_tool_calls(text: str) -> list[tuple[str, int]]:
    """Return ``(identifier, offset)`` for likely tool-name typos.

    A typo starts with a known tool prefix, is followed by ``(``, and
    is not itself in ``ALL_TOOLS``.
    """
    bad: list[tuple[str, int]] = []
    for ident, start in _iter_identifiers(text):
        if ident in _TOOL_BY_NAME:
            continue
        if not any(ident.startswith(prefix) for prefix in _TOOL_PREFIXES):
            continue
        after = _skip_inline_whitespace(text, start + len(ident))
        if after < len(text) and text[after] == "(":
            bad.append((ident, start))
    return bad


# ── Schema-aware validators ─────────────────────────────────────────────────


def _kwarg_value(kwargs: dict[str, str], key: str) -> str:
    """Return ``kwargs[key]`` stripped of surrounding whitespace and quotes."""
    return kwargs.get(key, "").strip().strip("\"'")


def _required_for_call(spec: ToolSpec, kwargs: dict[str, str]) -> set[str]:
    """Effective required-param set, applying the update-operation exception.

    ``propose_decision(operation="update")`` only accepts ``rationale`` +
    ``affected_decision_id`` at the server boundary, so ``title`` is
    relaxed from the schema's static required list.
    """
    required = set(spec["input_schema"].get("required", []))
    if spec["name"] == "propose_decision" and _kwarg_value(kwargs, "operation") == "update":
        required.discard("title")
    return required


def _call_looks_complete(positional: list[str], kwargs: dict[str, str]) -> bool:
    """Skip required-param checks for bare ``foo()`` references."""
    return bool(positional or kwargs)


# ── Parser self-tests ──────────────────────────────────────────────────────


def test_parser_extracts_positional_call():
    [call] = find_tool_calls("call `check_decision(proposed_approach)` first")
    assert call.name == "check_decision"
    assert call.args == "proposed_approach"


def test_parser_extracts_kwargs_with_quoted_value():
    [call] = find_tool_calls('propose_decision(title=..., operation="add", rejected=...)')
    positional, kwargs = _parse_args(call.args)
    assert positional == []
    assert set(kwargs) == {"title", "operation", "rejected"}
    assert kwargs["operation"] == '"add"'


def test_parser_splits_only_top_level_commas():
    [call] = find_tool_calls("propose_decision(title=foo, rejected=[a, b], operation=bar)")
    _, kwargs = _parse_args(call.args)
    assert set(kwargs) == {"title", "rejected", "operation"}


def test_parser_emits_unbalanced_calls_with_none_args():
    [call] = find_tool_calls("propose_decision(title=foo")
    assert call.args is None
    assert call.offset == len("propose_decision")


def test_required_for_call_relaxes_title_for_update_propose():
    spec = _TOOL_BY_NAME["propose_decision"]
    assert "title" in _required_for_call(spec, kwargs={})
    relaxed = _required_for_call(spec, kwargs={"operation": '"update"'})
    assert "title" not in relaxed
    assert "rationale" in relaxed


def test_tool_prefixes_derived_from_all_tools():
    """Sanity check that the misspelled-tool guard's prefix set covers ALL_TOOLS."""
    assert {"propose_", "check_", "get_"} <= set(_TOOL_PREFIXES)
    assert all(p.endswith("_") for p in _TOOL_PREFIXES)


# ── Structural drift assertions ────────────────────────────────────────────


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_surface_tool_calls_have_balanced_parens(surface_name: str, loader) -> None:
    """A known tool name followed by ``(`` must have a matching ``)``."""
    text = loader()
    unbalanced = [c for c in find_tool_calls(text) if c.args is None]
    if unbalanced:
        details = ", ".join(f"{c.name} at line {_line_number(text, c.offset)}" for c in unbalanced)
        pytest.fail(f"{surface_name}: unbalanced parens in tool call(s): {details}")


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_surface_tool_calls_have_no_phantom_params(surface_name: str, loader) -> None:
    """Every kwarg key and positional placeholder must name a real schema param."""
    text = loader()
    for call in _iter_well_formed_calls(text):
        schema_params = set(_TOOL_BY_NAME[call.name]["input_schema"].get("properties", {}))
        positional, kwargs = _parse_args(call.args)
        for name in (*kwargs, *positional):
            assert name in schema_params, (
                f"{_at(surface_name, text, call.offset)}: {call.name}({call.args}) "
                f"uses unknown param {name!r}; schema params are {sorted(schema_params)}"
            )


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_surface_tool_calls_supply_required_params(surface_name: str, loader) -> None:
    """Calls that look complete (any args at all) include every required param."""
    text = loader()
    for call in _iter_well_formed_calls(text):
        positional, kwargs = _parse_args(call.args)
        if not _call_looks_complete(positional, kwargs):
            continue
        required = _required_for_call(_TOOL_BY_NAME[call.name], kwargs)
        missing = required - set(positional) - set(kwargs)
        assert not missing, (
            f"{_at(surface_name, text, call.offset)}: {call.name}({call.args}) "
            f"is missing required params {sorted(missing)}; required = {sorted(required)}"
        )


# ── Explicit regressions for known-bad signatures ──────────────────────────

_RETIRED_PROPOSE_DECISION_POSITIONALS: tuple[str, ...] = (
    "title",
    "rationale",
    "rejected",
    "confidence",
)

# propose_decision(operation="update") only accepts a narrow set of fields.
# title / confidence / decision_type / reversibility / files_affected /
# rejected are all rejected at the server boundary — use
# operation="supersede" if any of those must change.
_UPDATE_ALLOWED_KWARGS: frozenset[str] = frozenset(
    {"project_id", "rationale", "operation", "affected_decision_id", "skip_validation"}
)


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_no_retired_propose_decision_positional_shape(surface_name: str, loader) -> None:
    """The positional ``(title, rationale, rejected, confidence)`` shape is retired.

    Detected via the parsed positional list (not a substring match) so
    cosmetic edits to the template still trip the guard. Complements the
    substring guard in ``test_skills_drift.RETIRED_PHRASES``.
    """
    text = loader()
    for call in _iter_well_formed_calls(text):
        if call.name != "propose_decision":
            continue
        positional, _ = _parse_args(call.args)
        if tuple(positional) == _RETIRED_PROPOSE_DECISION_POSITIONALS:
            pytest.fail(
                f"{_at(surface_name, text, call.offset)}: propose_decision({call.args}) "
                "uses the retired positional contract — the operation-aware "
                "signature is required"
            )


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_propose_decision_multi_kwarg_calls_specify_operation(surface_name: str, loader) -> None:
    """Every multi-kwarg propose_decision template must include ``operation``.

    The agent owns the add/update/supersede classification, so every full
    call must specify it. Calls with 0 or 1 kwarg are abbreviated
    references, not full templates.
    """
    text = loader()
    for call in _iter_well_formed_calls(text):
        if call.name != "propose_decision":
            continue
        _, kwargs = _parse_args(call.args)
        if len(kwargs) < 2:
            continue
        assert "operation" in kwargs, (
            f"{_at(surface_name, text, call.offset)}: propose_decision({call.args}) "
            "is missing the operation kwarg — operation-aware calls are required"
        )


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_propose_decision_update_kwargs_within_allowlist(surface_name: str, loader) -> None:
    """``propose_decision(operation="update")`` accepts only a narrow set.

    ``title``, ``confidence``, ``decision_type``, ``reversibility``,
    ``files_affected``, ``rejected`` are rejected at the server boundary
    for update — use ``operation="supersede"`` if any of those must change.
    """
    text = loader()
    for call in _iter_well_formed_calls(text):
        if call.name != "propose_decision":
            continue
        _, kwargs = _parse_args(call.args)
        if _kwarg_value(kwargs, "operation") != "update":
            continue
        extra = set(kwargs) - _UPDATE_ALLOWED_KWARGS
        assert not extra, (
            f"{_at(surface_name, text, call.offset)}: "
            f"propose_decision({call.args}) includes boundary-rejected fields "
            f'for operation="update": {sorted(extra)}; '
            f"allowed = {sorted(_UPDATE_ALLOWED_KWARGS)}"
        )


@pytest.mark.parametrize("surface_name,loader", _SURFACE_PARAMS)
def test_surface_has_no_misspelled_tool_calls(surface_name: str, loader) -> None:
    """Nauro-prefixed identifiers followed by ``(`` must be known tool names."""
    text = loader()
    bad = _find_misspelled_tool_calls(text)
    if bad:
        details = ", ".join(f"{ident!r} at line {_line_number(text, off)}" for ident, off in bad)
        pytest.fail(
            f"{surface_name}: misspelled or unknown Nauro-prefixed tool call(s): "
            f"{details}; known tools are {sorted(_TOOL_NAMES)}"
        )
