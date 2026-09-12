"""Every local tool answers a missing store with the no-project guidance envelope."""

import inspect
from pathlib import Path

import pytest

from nauro.cli.autogen import AUTOGEN_ALLOWLIST
from nauro.mcp import tools
from nauro.onboarding import WELCOME_NO_PROJECT

TOOLS = sorted(name for name in vars(tools) if name.startswith("tool_"))


def _placeholder(parameter: inspect.Parameter) -> object:
    return 1 if parameter.annotation in ("int", int) else "placeholder"


@pytest.mark.parametrize("name", TOOLS)
def test_missing_store_short_circuits_before_any_argument_is_used(tmp_path: Path, name: str):
    tool = getattr(tools, name)
    required = {
        parameter.name: _placeholder(parameter)
        for parameter in inspect.signature(tool).parameters.values()
        if parameter.default is inspect.Parameter.empty and parameter.name != "store_path"
    }
    envelope = tool(tmp_path / "missing", **required)
    assert envelope["store"] == "local"
    assert envelope["status"] == "error"
    assert envelope["guidance"] == WELCOME_NO_PROJECT


def test_guarded_tools_are_exactly_the_local_tool_allowlist():
    assert {name.removeprefix("tool_") for name in TOOLS} == set(AUTOGEN_ALLOWLIST)
