"""Frozen-contract snapshot for the 1.0 public surface (D318).

D318 took ``nauro`` and ``nauro-core`` to 1.0.0 and froze the *local product
surface*: the stdio MCP tool contract (names + schemas), the CLI command tree
(commands + flags), the on-disk store-format constants, and ``nauro-core``'s
curated public import API. After 1.0, breaking any of these requires a major
bump.

This test is the *mechanical* enforcement of that doctrinal promise. It
serializes the live contract and asserts it equals a checked-in snapshot. The
crucial property is that the expectation is **checked in, not derived from live
code** — unlike the sibling drift tests (``test_skill_tool_signatures`` derives
``_TOOL_NAMES`` from the live registry; ``cli.autogen`` verifies its allowlist
against live ``ALL_TOOLS``), which move *with* the code and so cannot, by
construction, catch a contract change. A pinned snapshot is the one test that
can disagree with the code, which is exactly what a freeze needs.

The test never decides major-vs-minor. It makes a contract change **loud**: an
intentional change is a deliberate snapshot regen, and the resulting diff (e.g.
``+ "required": ["proposed_approach", "scope"]``) is the trigger to ask "is this
a 2.0?" — a question for review (the @nauro-reviewer pass) against D318, not for
this assertion.

What is frozen here:
  - ``stdio_tool_specs``     — every ``ALL_TOOLS`` spec: name, annotations, and
                               the *structural* input schema (param names, types,
                               required, enums, defaults).
  - ``cli_command_tree``     — the Typer/Click command + subcommand tree with
                               each option's flags, type, required, choices.
  - ``cli_autogen_commands`` — the explicit set of tools mirrored as CLI
                               commands (``AUTOGEN_ALLOWLIST``).
  - ``nauro_core_public_api``— ``nauro_core.__all__``, the curated import surface
                               D318 promised (vs. the ~107 incidental exports).
  - ``public_constant_values``— the *values* of the store-format filenames,
                               schema version, size limits, and decision types
                               (renaming ``project.md`` breaks every store).

Deliberately OUT of scope (documented, not silent — coverage is the value):
  - Prose. Tool ``title``/``description`` and per-property descriptions are
    stripped: they are documentation, not a breaking contract, and including
    them trains regen-without-reading. Wording drift is owned by
    ``test_protocol_drift``.
  - Tool *output* models. ``check_decision``'s result schema is already pinned
    by ``test_check_decision_schema``; generalizing output models to the other
    tools is the natural next extension of that test, not this one.
  - The on-disk store *body* format beyond the constants above (the strict v2
    parser in ``nauro_core.decision_model`` owns it) and the stdio-vs-remote
    registration split (``list_projects`` is remote-only by design).

Regenerate after an intentional, reviewed contract change (note: ``.venv`` per
the project's test-env convention — ``uv run`` clobbers the shared venv), from
the ``nauro`` workspace root:

    NAURO_UPDATE_CONTRACT=1 .venv/bin/python -m pytest \\
        packages/nauro/tests/test_public_contract.py

then review the diff to ``snapshots/public_contract_v1.json`` carefully — a
``v2`` file is what a 2.0 looks like.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import nauro_core
import pytest
import typer
from nauro_core.mcp_tools import ALL_TOOLS

from nauro.cli.autogen import AUTOGEN_ALLOWLIST

CONTRACT_VERSION = 1
SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / f"public_contract_v{CONTRACT_VERSION}.json"
_UPDATE_ENV = "NAURO_UPDATE_CONTRACT"

# Schema annotation keys carrying prose, dropped so wording edits do not trip a
# structural freeze. NOT to be confused with property *names* (see _strip_prose).
_PROSE_KEYS = frozenset({"description", "title"})
# Schema keywords that are semantically sets — sorted for run-to-run determinism
# regardless of source ordering (reordering an enum is not a breaking change;
# adding or removing a value is, and still trips).
_SORTED_SCHEMA_KEYS = frozenset({"required", "enum"})

# Framework-injected CLI params excluded so the snapshot tracks *our* surface,
# not Typer/Click internals that shift on a dependency bump.
_FRAMEWORK_PARAMS = frozenset({"help", "install_completion", "show_completion"})

# nauro-core public names whose *value* (not just presence in __all__) is part
# of the contract: store-format filenames, schema version, write-path limits,
# and the decision-type vocabulary.
_CONTRACT_CONSTANTS: tuple[str, ...] = (
    "MAX_BRIEF_BYTES",
    "MAX_RATIONALE_LENGTH",
    "MAX_TITLE_LENGTH",
    "MAX_CONTEXT_LENGTH",
    "MAX_APPROACH_LENGTH",
    "MAX_DELTA_LENGTH",
    "MAX_QUESTION_LENGTH",
    "MIN_RATIONALE_LENGTH",
    "SNAPSHOT_SCHEMA_VERSION",
    "DECISIONS_DIR",
    "SNAPSHOTS_DIR",
    "DECISION_HASHES_FILE",
    "PROJECT_MD",
    "STACK_MD",
    "STATE_MD",
    "OPEN_QUESTIONS_MD",
    "STATE_CURRENT_FILENAME",
    "STATE_HISTORY_FILENAME",
    "DECISION_TYPES",
)


def _strip_prose(node: Any) -> Any:
    """Return ``node`` with schema prose removed, structure preserved.

    Drops ``description``/``title`` annotation keys at every schema level, but
    recurses into ``properties`` *values* while keeping their keys — so a
    property literally named ``description`` survives as a property; only a
    schema-level ``description`` annotation is removed. ``required``/``enum``
    lists are sorted for determinism. The input is never mutated.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _PROSE_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: _strip_prose(sub) for name, sub in value.items()}
            elif key in _SORTED_SCHEMA_KEYS and isinstance(value, list):
                out[key] = sorted(value, key=repr)
            else:
                out[key] = _strip_prose(value)
        return out
    if isinstance(node, list):
        return [_strip_prose(value) for value in node]
    return node


def _tool_specs() -> list[dict[str, Any]]:
    """Structural snapshot of every tool spec, sorted by name."""
    specs = [_strip_prose(dict(spec)) for spec in ALL_TOOLS]
    return sorted(specs, key=lambda spec: spec["name"])


def _param_node(param: click.Parameter) -> dict[str, Any]:
    node: dict[str, Any] = {
        "name": param.name,
        "kind": type(param).__name__,
        "type": getattr(param.type, "name", type(param.type).__name__),
        "required": bool(param.required),
    }
    if isinstance(param, click.Option):
        node["flags"] = sorted([*param.opts, *param.secondary_opts])
        if param.is_flag:
            node["is_flag"] = True
        if param.multiple:
            node["multiple"] = True
    if isinstance(param.type, click.Choice):
        node["choices"] = sorted(param.type.choices)
    # Record only JSON-literal defaults; None/callables/sentinels are omitted
    # (a stable omission rather than a volatile repr).
    if isinstance(param.default, (str, int, float, bool)):
        node["default"] = param.default
    return node


def _walk_cli(command: click.Command, name: str, ctx: click.Context) -> dict[str, Any]:
    node: dict[str, Any] = {
        "name": name,
        "kind": "group" if isinstance(command, click.Group) else "command",
    }
    if getattr(command, "hidden", False):
        node["hidden"] = True
    params = [_param_node(p) for p in command.params if p.name not in _FRAMEWORK_PARAMS]
    node["params"] = sorted(params, key=lambda p: p["name"])
    if isinstance(command, click.Group):
        children = []
        for sub_name in sorted(command.list_commands(ctx)):
            sub = command.get_command(ctx, sub_name)
            if sub is None:
                continue
            child_ctx = click.Context(sub, parent=ctx, info_name=sub_name)
            children.append(_walk_cli(sub, sub_name, child_ctx))
        node["commands"] = children
    return node


def _build_cli_tree() -> dict[str, Any]:
    """Walk the CLI command tree in-process. Runs inside the fresh subprocess
    spawned by :func:`_cli_tree`, where the app is pristine."""
    from nauro.cli.main import app

    command = typer.main.get_command(app)
    return _walk_cli(command, "nauro", click.Context(command, info_name="nauro"))


# Capture the CLI tree from a FRESH interpreter, not in-process. The tree is
# read off the process-global ``nauro.cli.main.app`` singleton, which other
# tests in the suite import and exercise (CliRunner invocations, registration
# assertions). An in-process read is therefore sensitive to suite ordering and
# prior mutation of that shared object — observed as the registered commands
# being absent under CI's ordering, collapsing the tree to an empty list. A
# subprocess is hermetic and matches how the real CLI runs (a fresh process),
# so the frozen surface is the surface a user actually gets. (Same fresh-process
# pattern as test_retrieval_bench_smoke.)
_CLI_TREE_DUMP = (
    "import json, sys; sys.path.insert(0, sys.argv[1]); "
    "import test_public_contract as _t; print(json.dumps(_t._build_cli_tree()))"
)


def _cli_tree() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", _CLI_TREE_DUMP, str(Path(__file__).parent)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to capture the CLI command tree in a subprocess:\n" + proc.stderr
        )
    return json.loads(proc.stdout)


def _constant_values() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _CONTRACT_CONSTANTS:
        value = getattr(nauro_core, name)
        if isinstance(value, (str, int, bool)):
            out[name] = value
        elif isinstance(value, (set, frozenset, tuple, list)):
            out[name] = sorted(value, key=repr)
        else:  # pragma: no cover - defensive; no such constant today
            out[name] = repr(value)
    return out


def build_contract() -> dict[str, Any]:
    """Assemble the full public-contract snapshot from live sources."""
    return {
        "contract_version": CONTRACT_VERSION,
        "stdio_tool_specs": _tool_specs(),
        "cli_command_tree": _cli_tree(),
        "cli_autogen_commands": sorted(AUTOGEN_ALLOWLIST),
        "nauro_core_public_api": sorted(nauro_core.__all__),
        "public_constant_values": _constant_values(),
    }


def _dumps(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True)


def test_public_contract_matches_snapshot() -> None:
    current = _dumps(build_contract())

    if os.environ.get(_UPDATE_ENV) == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(current + "\n", encoding="utf-8")
        pytest.skip(f"{SNAPSHOT_PATH.name} regenerated from the live contract")

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
                tofile="live contract (current)",
                lineterm="",
                n=2,
            )
        )
        if len(diff) > 8000:
            diff = diff[:8000] + "\n... (diff truncated)"
        raise AssertionError(
            "The 1.0 public contract has drifted from the checked-in snapshot "
            f"({SNAPSHOT_PATH.name}). This is a frozen surface under D318: a change "
            "here is a major-version (2.0) concern unless it is purely additive and "
            "you have confirmed so. If the change is intentional and reviewed, "
            f"regenerate with `{_UPDATE_ENV}=1 .venv/bin/python -m pytest "
            f"{Path(__file__).name}` and review the diff carefully.\n\n"
            f"{diff}"
        )
