"""Surface-level parity for the local ``list_decisions`` adapters.

After the kernel cutover, every local surface that exposes
``list_decisions`` must produce the same envelope for the same arguments
against the same store. This file pins that envelope shape across the
two adapter wirings that exist today.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. There is no ``nauro list-decisions`` command. Auto-gen
  of ``nauro tool <name>`` from MCP ToolSpecs is deferred; adding a
  hand-written CLI mirror now would be speculative.
* No FastAPI surface. The local server does not expose a
  ``/list_decisions`` endpoint. Adding one purely to mirror the parity
  shape would be speculative infrastructure; the cross-store layer-3
  test (``test_list_decisions_cross_surface``) covers local-vs-cloud
  parity once the cloud Store exists.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)
from nauro_core.renderers import RENDERERS

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp.stdio_server import list_decisions as stdio_list_decisions
from nauro.mcp.tools import tool_list_decisions
from nauro.onboarding import WELCOME_NO_PROJECT
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


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


@pytest.fixture
def empty_repo(tmp_path, monkeypatch):
    """Register a project with an empty store (no decisions) and chdir in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("empty-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "empty-project"})
    # Create the store path but no decisions/ subdir.
    store_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo)
    return pid, store_path


def _stdio_rendered(pid: str, *, limit: int = 20, include_superseded: bool = False) -> str:
    """Return the rendered stdio surface text for the parity comparison.

    Renderer-scoped read tools now return a single ``content[0]`` block
    carrying the renderer output. Parity against the direct tool envelope
    runs through ``RENDERERS["list_decisions"]``.
    """
    result = stdio_list_decisions(
        project_id=pid, limit=limit, include_superseded=include_superseded
    )
    assert len(result.content) == 1
    assert result.structuredContent is None
    return result.content[0].text


def _tool_envelope(store_path: Path, *, limit: int = 20, include_superseded: bool = False) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_list_decisions(store_path, limit, include_superseded)


def test_empty_store_envelope_matches_across_surfaces(empty_repo):
    pid, store_path = empty_repo
    tool = _tool_envelope(store_path)
    assert _stdio_rendered(pid) == RENDERERS["list_decisions"](tool)
    assert tool.pop("project")["id"] == pid
    assert tool == {"store": "local", "decisions": []}


def test_populated_store_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    tool = _tool_envelope(store_path)
    assert _stdio_rendered(pid) == RENDERERS["list_decisions"](tool)
    # Locked envelope shape: store + decisions list, no error / status keys.
    assert tool["store"] == "local"
    assert isinstance(tool["decisions"], list)
    assert tool["decisions"], "demo project seeds 7 active decisions"
    # All demo decisions are active and seeded with date + decision_type,
    # so every row carries the full set of optional keys.
    for row in tool["decisions"]:
        for key in ("number", "title", "date", "status", "type", "confidence"):
            assert key in row, f"missing field {key!r} in row {row!r}"
        assert row["status"] == "active"


def test_include_superseded_toggle_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    tool_default = _tool_envelope(store_path, include_superseded=False)
    assert _stdio_rendered(pid, include_superseded=False) == RENDERERS["list_decisions"](
        tool_default
    )

    tool_with = _tool_envelope(store_path, include_superseded=True)
    assert _stdio_rendered(pid, include_superseded=True) == RENDERERS["list_decisions"](tool_with)

    # Demo project has only active decisions, so both toggles return the
    # same envelope; the parity assertion is the load-bearing one.
    assert tool_default == tool_with


def test_exclude_none_strips_unset_type_across_surfaces(tmp_path, monkeypatch):
    """A row whose underlying ``decision_type`` is None omits ``type`` on both surfaces.

    Pins the ``exclude_none=True`` serialization contract at the surface
    layer: demo decisions all carry a ``decision_type``, so the strip
    direction was previously only covered by the kernel test. This case
    seeds a decision with no ``decision_type`` into a fresh
    ``FilesystemStore`` and asserts both the stdio and direct-tool
    envelopes drop the ``type`` key from that row.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("no-type-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "no-type-project"})
    decisions_dir = store_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    body = format_decision(
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
            num=1,
            title="Decision without a type",
            rationale="Seeded with decision_type unset to lock the exclude_none surface contract.",
        )
    )
    (decisions_dir / "001-decision-without-a-type.md").write_text(body)
    monkeypatch.chdir(repo)

    tool = _tool_envelope(store_path)
    assert _stdio_rendered(pid) == RENDERERS["list_decisions"](tool)
    assert len(tool["decisions"]) == 1
    row = tool["decisions"][0]
    assert "type" not in row
    assert row == {
        "number": 1,
        "title": "Decision without a type",
        "date": "2026-01-01",
        "status": "active",
        "confidence": "medium",
    }


def test_store_missing_guidance_branch_on_tool(tmp_path):
    """The tool returns the welcome guidance envelope when the store path is absent.

    Stdio cannot exercise this branch with the same envelope: when the
    caller passes a known ``project_id`` whose store is missing, the
    resolver raises ``StoreResolutionError`` with its own message before
    the tool ever runs. The branch covered here is the
    ``tool_list_decisions`` adapter's own missing-store check, which
    remote transports (cloud HTTP MCP) also rely on.
    """
    missing_store = tmp_path / "no-such-store"
    assert not missing_store.exists()
    envelope = _tool_envelope(missing_store)
    # Identity is best-effort: an unregistered/missing store falls back to the
    # directory name with no id.
    assert envelope.pop("project") == {"id": None, "name": "no-such-store"}
    assert envelope == {"store": "local", "status": "error", "guidance": WELCOME_NO_PROJECT}
