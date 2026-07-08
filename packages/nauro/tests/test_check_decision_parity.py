"""Surface-level parity for the local ``check_decision`` adapters.

After the kernel cutover, the local surfaces — CLI ``nauro check-decision``,
the local stdio MCP ``check_decision`` tool, and the transport-agnostic
``tool_check_decision`` adapter — must produce the same envelope for the same
proposal against the same store. This file pins the envelope shape across the
adapter wirings; the cross-store layer-3 test
(``test_check_decision_cross_surface``) covers local-vs-cloud parity once the
cloud Store exists.

The stdio MCP surface now returns a single rendered ``content[0]`` text block;
the parity assertion against the other surfaces is the identity
``stdio.content[0].text == RENDERERS["check_decision"](envelope)`` where
``envelope`` is the kernel result that drives every other surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nauro_core.renderers import RENDERERS
from typer.testing import CliRunner

from nauro.cli.main import app as cli_app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp.stdio_server import check_decision as stdio_check_decision
from nauro.mcp.tools import tool_check_decision
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

DEMO_PROMPT = "Store dollar amounts as decimal numbers"


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


def _cli_envelope(store_path: Path) -> dict:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _stdio_rendered(pid: str, proposed_approach: str = DEMO_PROMPT) -> str:
    """Return the rendered stdio surface text for the parity comparison.

    Renderer-scoped read tools now return a single ``content[0]`` block
    carrying the renderer output. The stdio surface participates in parity by
    emitting the same renderer output the other surfaces would produce for
    their (identical) kernel envelope.
    """
    result = stdio_check_decision(proposed_approach=proposed_approach, project_id=pid)
    assert len(result.content) == 1
    assert result.structuredContent is None
    return result.content[0].text


def _tool_envelope(store_path: Path) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_check_decision(store_path, DEMO_PROMPT)


# --- happy path: the local surfaces produce the same envelope ---------------


def test_cli_stdio_tool_match_on_success_path(demo_repo):
    pid, store_path = demo_repo
    cli = _cli_envelope(store_path)
    tool = _tool_envelope(store_path)
    assert cli == tool
    # stdio collapses to the rendered surface; parity is that the rendered
    # text is what the shared renderer would produce for the same envelope.
    assert _stdio_rendered(pid) == RENDERERS["check_decision"](tool)


def test_success_envelope_contains_canonical_fields(demo_repo):
    pid, store_path = demo_repo
    envelope = _tool_envelope(store_path)
    assert envelope["store"] == "local"
    assert "related_decisions" in envelope
    assert "assessment" in envelope
    # ``error`` field is omitted on the success path (exclude_none).
    assert "error" not in envelope
    # Each related decision carries the canonical id/title/score/status/date/preview.
    for hit in envelope["related_decisions"]:
        for key in ("id", "title", "score", "status", "date", "rationale_preview"):
            assert key in hit, f"missing canonical field {key!r} in hit {hit!r}"


# --- rejection: the local surfaces share the same error envelope ------------


def test_rejection_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    overlong = "x" * 10_000

    runner = CliRunner()
    cli_raw = runner.invoke(cli_app, ["check-decision", overlong])
    assert cli_raw.exit_code == 1, cli_raw.output
    cli = json.loads(cli_raw.stdout)

    tool = tool_check_decision(store_path, overlong)

    assert cli == tool
    # stdio surfaces the kernel rejection through the renderer's error block;
    # parity is that the rendered text matches what the renderer would emit for
    # the same envelope every other surface returned.
    stdio_rendered = _stdio_rendered(pid, overlong)
    assert stdio_rendered == RENDERERS["check_decision"](tool)
    # Locked rejection envelope shape for this operation.
    assert cli["store"] == "local"
    assert cli["related_decisions"] == []
    assert cli["assessment"] == ""
    assert cli["error"]["kind"] == "rejected"
    assert "exceeds" in cli["error"]["reason"].lower()
