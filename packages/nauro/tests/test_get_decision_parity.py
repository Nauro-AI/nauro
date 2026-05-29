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

from pathlib import Path

import pytest
from nauro_core.renderers import RENDERERS

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


def _stdio_rendered(pid: str, number: int, mode: str = "full") -> str:
    """Return the rendered stdio surface text for the parity comparison.

    Renderer-scoped read tools now return a single ``content[0]`` block
    carrying the renderer output. Parity against the direct tool envelope
    runs through ``RENDERERS["get_decision"]`` with the same ``mode``.
    """
    result = stdio_get_decision(number=number, project_id=pid, mode=mode)
    assert len(result.content) == 1
    assert result.structuredContent is None
    return result.content[0].text


def _tool_envelope(store_path: Path, number: int, mode: str = "full") -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_get_decision(store_path, number, mode)


def test_stdio_and_tool_match_on_success_path(demo_repo):
    pid, store_path = demo_repo
    tool = _tool_envelope(store_path, EXISTING_NUMBER)
    assert _stdio_rendered(pid, EXISTING_NUMBER) == RENDERERS["get_decision"](tool)


def test_success_envelope_carries_content(demo_repo):
    _pid, store_path = demo_repo
    envelope = _tool_envelope(store_path, EXISTING_NUMBER)
    assert envelope["store"] == "local"
    assert "content" in envelope
    assert envelope["content"]
    # ``error`` field is omitted on the success path (exclude_none).
    assert "error" not in envelope


def test_not_found_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    tool = _tool_envelope(store_path, MISSING_NUMBER)
    assert _stdio_rendered(pid, MISSING_NUMBER) == RENDERERS["get_decision"](tool)
    # Locked rejection envelope shape.
    assert tool["store"] == "local"
    assert "content" not in tool
    assert tool["error"]["kind"] == "error"
    assert str(MISSING_NUMBER) in tool["error"]["reason"]


# ── Backward-compat: default envelope is byte-identical, no extra key ──


def test_default_envelope_byte_identical_to_full(demo_repo):
    """The byte-identity gate: omitting ``mode`` equals ``mode="full"`` and
    the envelope carries no discriminator key beyond store/content."""
    _pid, store_path = demo_repo
    default = _tool_envelope(store_path, EXISTING_NUMBER)
    full = _tool_envelope(store_path, EXISTING_NUMBER, mode="full")
    assert default == full
    # Locked success envelope: store + content only. No "mode" key leaked.
    assert set(default) == {"store", "content", "project"}


# ── Header mode: parity across the stdio + tool surfaces ──


def test_stdio_and_tool_match_on_header_mode(demo_repo):
    pid, store_path = demo_repo
    tool = _tool_envelope(store_path, EXISTING_NUMBER, mode="header")
    assert _stdio_rendered(pid, EXISTING_NUMBER, mode="header") == RENDERERS["get_decision"](
        tool, mode="header"
    )


def test_header_envelope_more_compact_than_full(demo_repo):
    _pid, store_path = demo_repo
    full = _tool_envelope(store_path, EXISTING_NUMBER, mode="full")
    header = _tool_envelope(store_path, EXISTING_NUMBER, mode="header")
    assert len(header["content"]) < len(full["content"])
    # Header carries the same envelope key set — no discriminator field.
    assert set(header) == set(full)


def test_header_not_found_matches_full_miss_envelope(demo_repo):
    pid, store_path = demo_repo
    header_miss = _tool_envelope(store_path, MISSING_NUMBER, mode="header")
    full_miss = _tool_envelope(store_path, MISSING_NUMBER, mode="full")
    assert header_miss == full_miss
    assert _stdio_rendered(pid, MISSING_NUMBER, mode="header") == RENDERERS["get_decision"](
        header_miss, mode="header"
    )
