"""Surface-level parity for the local ``flag_question`` adapters.

After the kernel cutover, every local surface that exposes
``flag_question`` must produce the same envelope for the same arguments
against the same store. Three wirings participate here:

* ``tool_flag_question`` — the direct adapter call. Returns the canonical
  dict envelope every other surface is derived from.
* ``nauro flag-question`` — the CLI auto-gen command. Reaches the adapter
  through the Typer entry point and prints the envelope as JSON.
* The stdio MCP ``flag_question`` tool — for FastMCP compatibility this
  surface returns a human-readable string instead of the dict envelope.
  We assert it matches the canonical string rendered from the tool
  envelope; it does not participate in dict equality.

The HTTP FastAPI server exposes a ``/flag_question`` endpoint; its shape
is pinned separately in ``test_mcp.py``. Equality across the three
surfaces above is the parity guarantee at this layer.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app as cli_app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp import tools as mcp_tools
from nauro.mcp.stdio_server import flag_question as stdio_flag_question
from nauro.mcp.tools import tool_flag_question
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Suppress the best-effort cloud push so the parity layer stays local."""
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, and chdir into the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "parity-flag-question",
        [repo],
        mode=REPO_CONFIG_MODE_LOCAL,
    )
    save_repo_config(
        repo,
        {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-flag-question"},
    )
    scaffold_project_store("parity-flag-question", store_path)
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
    result = runner.invoke(cli_app, ["flag-question", *args])
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
    if envelope.get("hint"):
        return f"{envelope['hint']} The question has still been logged."
    return "Question flagged."


def test_ok_envelope_matches_across_tool_and_cli(seeded_repo):
    pid, store_path = seeded_repo

    tool_envelope = tool_flag_question(store_path, "Should we ship X?")
    exit_code, cli_envelope, output = _cli_envelope(["Should we ship Y?"])
    assert exit_code == 0, output
    # Every local surface now carries project identity alongside store.
    assert tool_envelope.pop("project")["id"] == pid
    assert cli_envelope.pop("project")["id"] == pid
    assert tool_envelope == {"store": "local", "status": "ok"}
    assert cli_envelope == {"store": "local", "status": "ok"}

    stdio_string = stdio_flag_question(question="Should we ship Z?", project_id=pid)
    assert stdio_string == _render_stdio_string({"status": "ok"})


def test_missing_store_guidance_matches_across_surfaces(missing_store):
    tool_envelope = tool_flag_question(missing_store, "anything?")
    assert tool_envelope["store"] == "local"
    assert tool_envelope["status"] == "error"
    assert "nauro init" in tool_envelope["guidance"]

    exit_code, _cli_envelope_dict, output = _cli_envelope(["anything?"])
    assert exit_code == 1
    assert "nauro init" in output


def test_length_rejection_matches_across_tool_and_cli(seeded_repo):
    pid, store_path = seeded_repo
    overlong = "x" * 10_000

    tool_envelope = tool_flag_question(store_path, overlong)
    assert tool_envelope["store"] == "local"
    assert tool_envelope["status"] == "rejected"
    assert tool_envelope["error"]["kind"] == "rejected"
    assert "exceeds" in tool_envelope["error"]["reason"].lower()

    exit_code, cli_envelope, output = _cli_envelope([overlong])
    # Length rejection surfaces as ``status: "rejected"`` which the auto-gen
    # exit-code branch treats as a successful envelope (non-error).
    assert exit_code == 0, output
    assert cli_envelope == tool_envelope
