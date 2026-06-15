"""Tests for the auto-generated ``nauro check-decision`` CLI command.

Covers:
- The demo prompt retrieves the canonical SSE-over-WebSocket decision.
- The auto-gen command emits a parseable JSON envelope on stdout.
- Project-resolution and rejection error paths exit non-zero with guidance.
- The CLI surface routes through the ``@mcp_tool`` adapter — both
  ``cli.command_invoked`` and ``mcp.tool_called`` (with ``transport=cli``)
  fire on a single invocation.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from tests.conftest import seed_consented_config

runner = CliRunner()


# Canonical demo prompt — pinned so README references and integration assertions
# share one source of truth. Used by test_demo_prompt_returns_sse_decision.
DEMO_PROMPT = "Add a WebSocket endpoint for live task updates"


@pytest.fixture
def demo_repo(tmp_path, monkeypatch):
    """Register a local-mode v2 demo project rooted at tmp_path/repo.

    Returns (project_name, project_id, store_path, repo_path). The cwd is
    moved into the repo so resolve_target_project picks the right project.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("demo-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "demo-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)
    return "demo-project", pid, store_path, repo


# --- happy path: demo prompt retrieves the SSE-over-WebSocket decision ------


def test_demo_prompt_returns_sse_decision(demo_repo):
    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    titles = [d["title"] for d in payload.get("related_decisions", [])]
    ids = [d["id"] for d in payload.get("related_decisions", [])]
    assert any("SSE over WebSocket" in t for t in titles)
    assert "decision-004" in ids


def test_demo_prompt_json_output_is_parseable(demo_repo):
    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["store"] == "local"
    assert "related_decisions" in payload
    assert "assessment" in payload


def test_json_output_has_no_pre_header(demo_repo):
    """Output must be valid JSON from the first character — no human
    header lines printed before it.
    """
    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0
    assert result.stdout.lstrip().startswith("{")


# --- error and empty-store paths --------------------------------------------


def test_no_project_resolution_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 1
    assert "No project found" in result.output


def test_rejected_status_exits_nonzero(demo_repo):
    """Input over MAX_APPROACH_LENGTH must exit 1 with a rejection envelope."""
    overlong = "x" * 10_000
    result = runner.invoke(app, ["check-decision", overlong])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    # Rejection envelope: structured error block.
    assert payload["error"]["kind"] == "rejected"
    assert "reason" in payload["error"]


# --- telemetry: both cli.command_invoked and mcp.tool_called fire -----------


@pytest.fixture
def telemetry_enabled(tmp_path, monkeypatch):
    """Seed NAURO_HOME with a consented config so capture() actually fires."""
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")
    return seed_consented_config(tmp_path, enabled=True)


def test_cli_check_emits_mcp_tool_called_with_cli_transport(
    tmp_path, monkeypatch, telemetry_enabled, fake_posthog
):
    """The auto-gen CLI surface routes through the ``@mcp_tool`` adapter,
    so ``mcp.tool_called`` fires with ``transport=cli`` alongside the
    auto-instrumented ``cli.command_invoked`` event.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("telem-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "telem-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0, result.output

    event_names = [e["event"] for e in fake_posthog.events]
    assert "cli.command_invoked" in event_names
    assert "mcp.tool_called" in event_names

    cli_event = next(e for e in fake_posthog.events if e["event"] == "cli.command_invoked")
    assert cli_event["properties"]["command"] == "check-decision"

    mcp_event = next(e for e in fake_posthog.events if e["event"] == "mcp.tool_called")
    assert mcp_event["properties"]["tool_name"] == "check_decision"
    assert mcp_event["properties"]["transport"] == "cli"
