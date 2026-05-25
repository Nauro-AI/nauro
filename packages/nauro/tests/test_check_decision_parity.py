"""Surface-level parity for the local ``check_decision`` adapters.

After the kernel cutover, all three local surfaces (CLI
``nauro check-decision``, local stdio MCP ``check_decision`` tool,
FastAPI ``/check_decision``) must produce the same envelope for the
same proposal against the same store. This file pins the envelope
shape across the three adapter wirings; the cross-store layer-3 test
(``test_check_decision_cross_surface``) covers local-vs-cloud parity
once the cloud Store exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nauro.cli.main import app as cli_app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp.server import app as fastapi_app
from nauro.mcp.stdio_server import check_decision as stdio_check_decision
from nauro.mcp.tools import tool_check_decision
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

DEMO_PROMPT = "Add a WebSocket endpoint for live task updates"


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


def _stdio_envelope(pid: str) -> dict:
    # stdio check_decision returns a CallToolResult carrying both the
    # two-block content list and a typed structuredContent envelope —
    # see stdio_server module docstring for the contract. The two
    # surfaces must mirror each other; this helper validates parity
    # while returning the canonical envelope dict for the cross-surface
    # comparison.
    result = stdio_check_decision(proposed_approach=DEMO_PROMPT, project_id=pid)
    envelope = json.loads(result.content[1].text)
    assert result.structuredContent == envelope
    return envelope


def _http_envelope(pid: str) -> dict:
    client = TestClient(fastapi_app)
    response = client.post(
        "/check_decision",
        json={"project_id": pid, "proposed_approach": DEMO_PROMPT},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_envelope(store_path: Path) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_check_decision(store_path, DEMO_PROMPT)


# --- happy path: all three surfaces produce the same envelope ---------------


def test_cli_stdio_http_match_on_success_path(demo_repo):
    pid, store_path = demo_repo
    cli = _cli_envelope(store_path)
    stdio = _stdio_envelope(pid)
    http = _http_envelope(pid)
    tool = _tool_envelope(store_path)
    assert cli == stdio == http == tool


def test_success_envelope_contains_d141_canonical_fields(demo_repo):
    pid, _ = demo_repo
    envelope = _stdio_envelope(pid)
    assert envelope["store"] == "local"
    assert "related_decisions" in envelope
    assert "assessment" in envelope
    # ``error`` field is omitted on the success path (exclude_none).
    assert "error" not in envelope
    # Each related decision carries the canonical id/title/score/status/date/preview.
    for hit in envelope["related_decisions"]:
        for key in ("id", "title", "score", "status", "date", "rationale_preview"):
            assert key in hit, f"missing canonical field {key!r} in hit {hit!r}"


# --- rejection: all three surfaces share the same error envelope ------------


def test_rejection_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    overlong = "x" * 10_000

    runner = CliRunner()
    cli_raw = runner.invoke(cli_app, ["check-decision", overlong])
    assert cli_raw.exit_code == 1, cli_raw.output
    cli = json.loads(cli_raw.stdout)

    stdio_result = stdio_check_decision(proposed_approach=overlong, project_id=pid)
    stdio = json.loads(stdio_result.content[1].text)
    assert stdio_result.structuredContent == stdio

    client = TestClient(fastapi_app)
    response = client.post(
        "/check_decision",
        json={"project_id": pid, "proposed_approach": overlong},
    )
    assert response.status_code == 200, response.text
    http = response.json()

    tool = tool_check_decision(store_path, overlong)

    assert cli == stdio == http == tool
    # Locked rejection envelope shape (closes D141 for this operation).
    assert cli["store"] == "local"
    assert cli["related_decisions"] == []
    assert cli["assessment"] == ""
    assert cli["error"]["kind"] == "rejected"
    assert "exceeds" in cli["error"]["reason"].lower()
