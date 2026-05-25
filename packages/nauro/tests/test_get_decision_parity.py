"""Surface-level parity for the local ``get_decision`` adapters.

After the kernel cutover, every local surface that exposes
``get_decision`` must produce the same envelope for the same number
against the same store. This file pins that envelope shape across the
two adapter wirings that exist today.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. There is no ``nauro get-decision`` command. Auto-gen
  of ``nauro tool <name>`` from MCP ToolSpecs is deferred; adding a
  hand-written CLI mirror now would be speculative.
* No FastAPI surface. The local server does not expose a
  ``/get_decision`` endpoint. Adding one purely to mirror the parity
  shape would be speculative infrastructure; the cross-store layer-3
  test (``test_get_decision_cross_surface``) covers local-vs-cloud
  parity once the cloud Store exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp.stdio_server import get_decision as stdio_get_decision
from nauro.mcp.tools import tool_get_decision
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

EXISTING_NUMBER = 4
MISSING_NUMBER = 999


@pytest.fixture
def demo_repo(tmp_path, monkeypatch):
    """Seed a demo project + chdir into the repo so cwd resolution wins."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)
    return pid, store_path


def _stdio_envelope(pid: str, number: int) -> dict:
    # stdio get_decision now returns a two-block list[TextContent]; the
    # JSON envelope is at content[1].text — see stdio_server module
    # docstring for the contract.
    blocks = stdio_get_decision(number=number, project_id=pid)
    return json.loads(blocks[1].text)


def _tool_envelope(store_path: Path, number: int) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_get_decision(store_path, number)


def test_stdio_and_tool_match_on_success_path(demo_repo):
    pid, store_path = demo_repo
    stdio = _stdio_envelope(pid, EXISTING_NUMBER)
    tool = _tool_envelope(store_path, EXISTING_NUMBER)
    assert stdio == tool


def test_success_envelope_carries_content(demo_repo):
    pid, _ = demo_repo
    envelope = _stdio_envelope(pid, EXISTING_NUMBER)
    assert envelope["store"] == "local"
    assert "content" in envelope
    assert envelope["content"]
    # ``error`` field is omitted on the success path (exclude_none).
    assert "error" not in envelope


def test_not_found_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    stdio = _stdio_envelope(pid, MISSING_NUMBER)
    tool = _tool_envelope(store_path, MISSING_NUMBER)
    assert stdio == tool
    # Locked rejection envelope shape.
    assert stdio["store"] == "local"
    assert "content" not in stdio
    assert stdio["error"]["kind"] == "error"
    assert str(MISSING_NUMBER) in stdio["error"]["reason"]
