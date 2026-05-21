"""Tests for the ``nauro check`` CLI command.

Covers:
- The demo prompt retrieves the canonical SSE-over-WebSocket decision.
- ``--json`` emits a parseable result with the same shape as the human path.
- Project-resolution and store-state error paths exit non-zero with guidance.
- Cloud-mode projects surface a "may be stale" notice on stderr.
- The CLI does NOT emit ``mcp.tool_called`` telemetry — it calls the kernel
  operation directly so only the auto-instrumented ``cli.command_invoked``
  event fires.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_CLOUD, REPO_CONFIG_MODE_LOCAL
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


@pytest.fixture
def no_decisions_repo(tmp_path, monkeypatch):
    """Register a v2 project whose store has no decisions/ directory at all.

    Hits the empty-store branch in ``check_decision`` — distinct from a
    scaffolded store (which seeds 001-initial-setup.md and therefore has
    decisions).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("bare-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "bare-project"})
    # register_project_v2 already created the store dir; deliberately skip
    # scaffold_project_store so decisions/ never gets populated.
    monkeypatch.chdir(repo)
    return "bare-project", pid, store_path, repo


# --- happy path: demo prompt retrieves the SSE-over-WebSocket decision ------


def test_demo_prompt_returns_sse_decision(demo_repo):
    result = runner.invoke(app, ["check", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    # The canonical demo conflict is D004 (SSE over WebSocket).
    assert "D004" in result.output
    assert "SSE over WebSocket" in result.output
    # Header lines we want users to see.
    assert "store:" in result.output
    assert "project:" in result.output
    assert "approach:" in result.output
    assert "Related decisions" in result.output
    # Tail line points at decision files, not a non-existent CLI command.
    assert "decisions/" in result.output
    assert "get_decision MCP tool" in result.output


def test_demo_prompt_json_output_is_parseable(demo_repo):
    result = runner.invoke(app, ["check", DEMO_PROMPT, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["store"] == "local"
    assert "related_decisions" in payload
    assert "assessment" in payload
    titles = [d["title"] for d in payload["related_decisions"]]
    assert any("SSE over WebSocket" in t for t in titles)


def test_json_output_has_no_pre_header(demo_repo):
    """--json output must be valid JSON from the first character — no human
    header lines printed before it.
    """
    result = runner.invoke(app, ["check", DEMO_PROMPT, "--json"])
    assert result.exit_code == 0
    assert result.output.lstrip().startswith("{")


# --- error and empty-store paths --------------------------------------------


def test_no_project_resolution_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", DEMO_PROMPT])
    assert result.exit_code == 1
    assert "No project found" in result.output


def test_no_decisions_store_returns_no_decisions_message(no_decisions_repo):
    """A store with no decisions/ directory hits the NO_DECISIONS_TO_CHECK
    branch in the operation and surfaces the onboarding hint."""
    result = runner.invoke(app, ["check", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    # The NO_DECISIONS_TO_CHECK constant points the user at `nauro note`.
    assert "nauro note" in result.output or "decisions" in result.output.lower()
    # Header still emits.
    assert "store:" in result.output
    assert "project:" in result.output


def test_rejected_status_exits_nonzero_human(demo_repo):
    """Input over MAX_APPROACH_LENGTH must exit 1 with the rejection reason."""
    overlong = "x" * 10_000
    result = runner.invoke(app, ["check", overlong])
    assert result.exit_code == 1
    assert "exceeds" in result.output.lower() or "too long" in result.output.lower()


def test_rejected_status_exits_nonzero_json(demo_repo):
    """Same rejection contract as the human path — a script piping --json and
    checking $? must not see input-too-long as success.
    """
    overlong = "x" * 10_000
    result = runner.invoke(app, ["check", overlong, "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    # Rejection envelope: structured error block, no top-level "status".
    assert payload["error"]["kind"] == "rejected"
    assert "reason" in payload["error"]
    assert "status" not in payload


# --- cloud-mode notice -------------------------------------------------------


def test_cloud_mode_project_prints_stale_notice(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "cloud-project",
        [repo],
        mode=REPO_CONFIG_MODE_CLOUD,
        project_id="01H" + "A" * 23,
        server_url="https://example.invalid",
    )
    save_repo_config(
        repo,
        {
            "mode": REPO_CONFIG_MODE_CLOUD,
            "id": pid,
            "name": "cloud-project",
            "server_url": "https://example.invalid",
        },
    )
    create_demo_project(store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["check", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    # Notice goes to stderr but the CliRunner merges streams by default.
    assert "syncs to cloud" in result.output
    assert "local copy" in result.output


# --- telemetry: no mcp.tool_called from the CLI surface -----------------------


@pytest.fixture
def telemetry_enabled(tmp_path, monkeypatch):
    """Seed NAURO_HOME with a consented config so capture() actually fires."""
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")
    return seed_consented_config(tmp_path, enabled=True)


def test_cli_check_emits_no_mcp_tool_called_event(
    tmp_path, monkeypatch, telemetry_enabled, fake_posthog
):
    """The CLI path skips the @mcp_tool decorator's mcp.tool_called emission.

    Guards the helper-extraction refactor: if a future change reverts the CLI
    to calling tool_check_decision directly, this test fails.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("telem-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "telem-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["check", DEMO_PROMPT])
    assert result.exit_code == 0, result.output

    event_names = [e["event"] for e in fake_posthog.events]
    assert "mcp.tool_called" not in event_names
    # The Typer auto-instrumentation should still emit cli.command_invoked.
    assert "cli.command_invoked" in event_names
    cli_event = next(e for e in fake_posthog.events if e["event"] == "cli.command_invoked")
    assert cli_event["properties"]["command"] == "check"
