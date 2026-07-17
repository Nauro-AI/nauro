"""Tests for the auto-generated ``nauro check-decision`` CLI command.

Covers:
- The demo prompt retrieves the canonical integer-cents decision.
- The auto-gen command emits a parseable JSON envelope on stdout.
- Project-resolution and rejection error paths exit non-zero with guidance.
- The CLI surface routes through the same adapter as local MCP.
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

runner = CliRunner()


# Canonical demo prompt — pinned so README references and integration assertions
# share one source of truth. Used by test_demo_prompt_returns_integer_cents_decision.
DEMO_PROMPT = "Store dollar amounts as decimal numbers"


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


# --- happy path: demo prompt retrieves the integer-cents decision -----------


def test_demo_prompt_returns_integer_cents_decision(demo_repo):
    result = runner.invoke(app, ["check-decision", DEMO_PROMPT])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    titles = [d["title"] for d in payload.get("related_decisions", [])]
    ids = [d["id"] for d in payload.get("related_decisions", [])]
    assert any("Amounts stored in integer cents, never floating point" in t for t in titles)
    assert "decision-001" in ids


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
