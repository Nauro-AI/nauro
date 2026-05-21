"""Surface-level parity for the local ``get_raw_file`` adapters.

After the kernel cutover, every local surface that exposes
``get_raw_file`` must produce the same envelope for the same path
against the same store. This file pins that envelope shape across the
two adapter wirings that exist today.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. There is no ``nauro get-raw-file`` command. Auto-gen
  of ``nauro tool <name>`` from MCP ToolSpecs is deferred; adding a
  hand-written CLI mirror now would be speculative.
* No FastAPI surface. The local server does not expose a
  ``/get_raw_file`` endpoint. Adding one purely to mirror the parity
  shape would be speculative infrastructure; the cross-store layer-3
  test (``test_get_raw_file_cross_surface``) covers local-vs-cloud
  parity once the cloud Store exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.mcp.stdio_server import get_raw_file as stdio_get_raw_file
from nauro.mcp.tools import tool_get_raw_file
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

EXISTING_PATH = "project.md"
MISSING_PATH = "does-not-exist.md"
TRAVERSAL_PATH = "../../etc/passwd"


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


def _stdio_envelope(pid: str, path: str) -> dict:
    return stdio_get_raw_file(path=path, project_id=pid)


def _tool_envelope(store_path: Path, path: str) -> dict:
    """Direct call to the local tool, no transport wrapper."""
    return tool_get_raw_file(store_path, path)


def test_stdio_and_tool_match_on_success_path(demo_repo):
    pid, store_path = demo_repo
    stdio = _stdio_envelope(pid, EXISTING_PATH)
    tool = _tool_envelope(store_path, EXISTING_PATH)
    assert stdio == tool


def test_success_envelope_carries_content(demo_repo):
    pid, _ = demo_repo
    envelope = _stdio_envelope(pid, EXISTING_PATH)
    assert envelope["store"] == "local"
    assert "content" in envelope
    assert envelope["content"]
    # ``error`` and ``available_files`` are omitted on the success path.
    assert "error" not in envelope
    assert "available_files" not in envelope


def test_not_found_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    stdio = _stdio_envelope(pid, MISSING_PATH)
    tool = _tool_envelope(store_path, MISSING_PATH)
    assert stdio == tool
    # Locked miss envelope shape.
    assert stdio["store"] == "local"
    assert "content" not in stdio
    assert stdio["error"]["kind"] == "error"
    assert stdio["error"]["reason"] == f"File not found: {MISSING_PATH}"
    # Adapter adds the available_files hint as a sibling field, not
    # inside the error reason — kept structured so clients can render
    # the hint independently of the error message.
    assert isinstance(stdio["available_files"], list)
    assert all(isinstance(p, str) for p in stdio["available_files"])
    assert EXISTING_PATH in stdio["available_files"]


def test_traversal_envelope_matches_across_surfaces(demo_repo):
    pid, store_path = demo_repo
    stdio = _stdio_envelope(pid, TRAVERSAL_PATH)
    tool = _tool_envelope(store_path, TRAVERSAL_PATH)
    assert stdio == tool
    # Traversal is rejected by the adapter before any kernel call;
    # reason text is distinct from the kernel-side "File not found".
    assert stdio["store"] == "local"
    assert "content" not in stdio
    assert "available_files" not in stdio
    assert stdio["error"]["kind"] == "error"
    assert stdio["error"]["reason"] == f"Invalid path: {TRAVERSAL_PATH}"
