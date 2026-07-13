"""Surface-level parity for the local ``update_state`` adapters.

After the kernel cutover, every local surface that exposes
``update_state`` must produce the same envelope for the same arguments
against the same store. Three wirings participate here:

* ``tool_update_state`` — the direct adapter call. Returns the canonical
  dict envelope every other surface is derived from.
* ``nauro update-state`` — the CLI auto-gen command. Reaches the adapter
  through the Typer entry point and prints the envelope as JSON.
* The stdio MCP ``update_state`` tool — for FastMCP compatibility this
  surface returns a human-readable string instead of the dict envelope.
  We assert it matches the canonical string rendered from the tool
  envelope; it does not participate in dict equality.

The HTTP FastAPI server does not expose an ``/update_state`` endpoint;
equality across the three surfaces above is the parity guarantee at
this layer.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app as cli_app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp import tools as mcp_tools
from nauro.mcp.stdio_server import update_state as stdio_update_state
from nauro.mcp.tools import tool_update_state
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import register_v2_repo


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Suppress the best-effort cloud push so the parity layer stays local."""
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, and chdir into the repo."""
    result = register_v2_repo(tmp_path, "parity-update-state", monkeypatch=monkeypatch)
    return result.pid, result.store_path


@pytest.fixture
def overlap_repo(tmp_path, monkeypatch):
    """Repo whose state_current.md carries a bullet primed for keyword overlap."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "parity-update-state-overlap",
        [repo],
        mode=REPO_CONFIG_MODE_LOCAL,
    )
    save_repo_config(
        repo,
        {
            "mode": REPO_CONFIG_MODE_LOCAL,
            "id": pid,
            "name": "parity-update-state-overlap",
        },
    )
    scaffold_project_store("parity-update-state-overlap", store_path)
    (store_path / "state_current.md").write_text("- Implemented OAuth login flow with PKCE\n")
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def missing_store(tmp_path, monkeypatch):
    """A repo with no associated project store at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    nonexistent = tmp_path / "projects" / "nope"
    monkeypatch.chdir(repo)
    return nonexistent


def _cli_envelope(args: list[str]) -> tuple[int, dict | None, str]:
    """Invoke the auto-gen CLI command and parse the JSON envelope."""
    runner = CliRunner()
    result = runner.invoke(cli_app, ["update-state", *args])
    envelope: dict | None
    if result.stdout.strip():
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            envelope = None
    else:
        envelope = None
    return result.exit_code, envelope, result.output


def _render_stdio_string(envelope: dict) -> str:
    """Mirror the stdio surface's dict-to-string rendering rule."""
    if envelope.get("status") == "error":
        return envelope.get("guidance", "")
    if envelope.get("warning"):
        return f"State updated. {envelope['warning']}"
    return "State updated."


def test_ok_envelope_matches_across_tool_and_cli(seeded_repo):
    pid, store_path = seeded_repo

    tool_envelope = tool_update_state(store_path, "Shipped the parity test")
    # Each surface mutates the same store, so seed a fresh delta per surface
    # to keep the envelope shape the same (no warning) and exercise the rotation.
    exit_code, cli_envelope, output = _cli_envelope(["Shipped the CLI surface"])
    assert exit_code == 0, output
    # Every local surface now carries project identity alongside store.
    assert tool_envelope.pop("project")["id"] == pid
    assert cli_envelope.pop("project")["id"] == pid
    assert tool_envelope == {"store": "local", "status": "ok"}
    assert cli_envelope == {"store": "local", "status": "ok"}

    stdio_string = stdio_update_state(delta="Shipped the stdio surface", project_id=pid)
    assert stdio_string == _render_stdio_string({"status": "ok"})


def test_warning_envelope_matches_across_tool_and_cli(overlap_repo):
    pid, store_path = overlap_repo

    tool_envelope = tool_update_state(store_path, "Implemented OAuth refresh logic with PKCE")
    assert tool_envelope["store"] == "local"
    assert tool_envelope["status"] == "ok"
    assert "warning" in tool_envelope
    assert "keywords" in tool_envelope["warning"].lower()

    # Re-seed the overlap line because the previous tool call rotated it
    # into state_history.md.
    (store_path / "state_current.md").write_text("- Implemented OAuth login flow with PKCE\n")
    exit_code, cli_envelope, output = _cli_envelope(["Implemented OAuth refresh logic with PKCE"])
    assert exit_code == 0, output
    assert cli_envelope == tool_envelope

    (store_path / "state_current.md").write_text("- Implemented OAuth login flow with PKCE\n")
    stdio_string = stdio_update_state(
        delta="Implemented OAuth refresh logic with PKCE",
        project_id=pid,
    )
    assert stdio_string == _render_stdio_string(tool_envelope)


def test_missing_store_guidance_matches_across_surfaces(missing_store):
    tool_envelope = tool_update_state(missing_store, "anything")
    assert tool_envelope["store"] == "local"
    assert tool_envelope["status"] == "error"
    assert "nauro init" in tool_envelope["guidance"]

    # CLI auto-gen prints the envelope to stdout, then exits 1 with the
    # guidance routed to stderr.
    exit_code, cli_envelope, output = _cli_envelope(["anything"])
    assert exit_code == 1
    assert "nauro init" in output


def test_length_rejection_matches_across_tool_and_cli(seeded_repo):
    pid, store_path = seeded_repo
    overlong = "x" * 10_000

    tool_envelope = tool_update_state(store_path, overlong)
    assert tool_envelope["store"] == "local"
    assert tool_envelope["status"] == "rejected"
    assert tool_envelope["error"]["kind"] == "rejected"
    assert "exceeds" in tool_envelope["error"]["reason"].lower()

    exit_code, cli_envelope, output = _cli_envelope([overlong])
    # The auto-gen exit-code branch fires on the ``error`` / ``status: "error"``
    # paths; a length rejection surfaces as ``status: "rejected"`` which exits 0
    # but still carries the rejection reason in the printed envelope.
    assert exit_code == 0, output
    assert cli_envelope == tool_envelope
