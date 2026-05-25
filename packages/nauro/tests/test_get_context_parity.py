"""Surface-level parity for the local ``get_context`` adapters.

After the kernel cutover, every local surface that exposes
``get_context`` must produce the same envelope for the same arguments
against the same store. The two wirings under test today are the
``tool_get_context`` direct call and the stdio MCP wrapper that maps
``project_id``/``cwd`` onto a store path.

Two compressions vs. the ``check_decision`` parity test:

* No CLI surface. There is no ``nauro get-context`` command — auto-gen
  of ``nauro tool <name>`` from MCP ToolSpecs is deferred; a
  hand-written CLI mirror now would be speculative.
* No FastAPI surface as a parity participant. The local server's
  ``/context`` endpoint wraps the envelope inside a ``{"level": N,
  "content": <dict>}`` body so the byte-identical envelope assertion
  belongs to ``test_mcp`` instead; equality across stdio and direct tool
  is enough at this layer.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    format_decision,
)

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp.stdio_server import get_context as stdio_get_context
from nauro.mcp.tools import tool_get_context
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


def _seed(store_path: Path, *decisions: Decision) -> None:
    decisions_dir = store_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for d in decisions:
        slug = d.title.lower().replace(" ", "-")
        (decisions_dir / f"{d.num:03d}-{slug}.md").write_text(format_decision(d))


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Seed a project with two decisions plus the canonical store files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("parity-context", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-context"})
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / "project.md").write_text("# Project\n\nGoal: ship the thing.\n")
    (store_path / "state_current.md").write_text("# Current State\n\n- Shipped Postgres adoption\n")
    (store_path / "stack.md").write_text("# Stack\n- **Python 3.11** — primary language\n")
    (store_path / "open-questions.md").write_text("# Open Questions\n- [Q1] Should we add Redis?\n")
    _seed(
        store_path,
        Decision(
            date=date(2026, 1, 1),
            confidence=DecisionConfidence.high,
            status=DecisionStatus.active,
            num=1,
            title="Use Auth0 for authentication",
            rationale="Auth0 provides OAuth 2.1 support and handles JWT validation.",
        ),
        Decision(
            date=date(2026, 1, 2),
            confidence=DecisionConfidence.medium,
            status=DecisionStatus.active,
            num=2,
            title="Use FastAPI for MCP server",
            rationale="FastAPI plus Mangum is the Lambda deployment combination.",
        ),
    )
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def empty_repo(tmp_path, monkeypatch):
    """Register a project whose store has no decision content yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("empty-context", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "empty-context"})
    store_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def missing_store(tmp_path, monkeypatch):
    """A repo with no associated Nauro project store at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    nonexistent = tmp_path / "projects" / "nope"
    monkeypatch.chdir(repo)
    return nonexistent


def _stdio_envelope(pid: str, level) -> dict:
    # stdio get_context now returns a two-block list[TextContent]; the
    # JSON envelope is at content[1].text — see stdio_server module
    # docstring for the contract.
    blocks = stdio_get_context(project_id=pid, level=level)
    return json.loads(blocks[1].text)


def _tool_envelope(store_path: Path, level) -> dict:
    return tool_get_context(store_path, level)


@pytest.mark.parametrize("level", ["L0", "L1", "L2"])
def test_hit_envelope_matches_across_surfaces(seeded_repo, level):
    pid, store_path = seeded_repo
    stdio = _stdio_envelope(pid, level)
    tool = _tool_envelope(store_path, level)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert isinstance(stdio["content"], str)
    assert "Use Auth0" in stdio["content"]


def test_empty_store_envelope_matches_across_surfaces(empty_repo):
    pid, store_path = empty_repo
    stdio = _stdio_envelope(pid, "L0")
    tool = _tool_envelope(store_path, "L0")
    assert stdio == tool
    # Empty stores surface the NO_CONTEXT_YET trailer via the content body;
    # the envelope itself stays the dict shape.
    assert stdio["store"] == "local"
    assert "no context data yet" in stdio["content"] or "propose_decision" in stdio["content"]


def test_invalid_level_rejection_matches_across_surfaces(seeded_repo):
    pid, store_path = seeded_repo
    # Use a numeric level outside {0,1,2} so _coerce_level passes it through
    # and the kernel surfaces the rejection.
    stdio = _stdio_envelope(pid, 7)
    tool = _tool_envelope(store_path, 7)
    assert stdio == tool
    assert stdio["store"] == "local"
    assert stdio["error"]["kind"] == "rejected"
    assert "Invalid level" in stdio["error"]["reason"]


def test_missing_store_guidance_matches_across_surfaces(missing_store):
    # Direct tool call against a nonexistent store path mirrors the stdio
    # WELCOME_NO_PROJECT response shape.
    tool = _tool_envelope(missing_store, "L0")
    assert tool["store"] == "local"
    assert tool["status"] == "error"
    assert "nauro init" in tool["guidance"]


# --- Byte-identical content parity against the pre-cutover baseline ---
#
# The baseline below is a snapshot of ``tool_get_context`` output taken
# from ``main`` against the fixture seeded by ``_seed_baseline_store``.
# Captured from ``tool_get_context`` against the seeded fixture at main
# commit 3b9cffb (pre-cutover, ``feat(operations): cut search_decisions
# over to nauro_core kernel``). The kernel + transport rewire must
# reproduce it character-for-character; any drift indicates either a
# transport-decoration regression (Last-synced trailer, snapshot diff,
# NO_CONTEXT_YET sentinel) or a kernel content change.

_BASELINE_L0 = """## Current State
# Current State

**Sprint:** ship beta
**Blocker:** none

- Shipped MCP cutovers PR2-5

**Stack:** Python 3.11, FastAPI, PostgreSQL

## Open Questions
- [Q1] Do we add Redis for caching?

## Recent Decisions
- D2 — Use FastAPI (2026-01-02)
- D1 — Adopt Postgres (2026-01-01)"""


def _seed_baseline_store(store_path: Path) -> None:
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / "decisions").mkdir(parents=True, exist_ok=True)
    (store_path / "snapshots").mkdir(parents=True, exist_ok=True)
    (store_path / "project.md").write_text(
        "# Project\n\nGoal: ship Nauro CLI v1 with strong opinions.\n"
    )
    (store_path / "state_current.md").write_text(
        "# Current State\n\n"
        "**Sprint:** ship beta\n"
        "**Blocker:** none\n"
        "\n"
        "- Shipped MCP cutovers PR2-5\n"
    )
    (store_path / "stack.md").write_text(
        "# Stack\n\n"
        "- **Python 3.11** — primary language\n"
        "- **FastAPI** — HTTP framework\n"
        "- **PostgreSQL** — primary store\n"
    )
    (store_path / "open-questions.md").write_text(
        "# Open Questions\n\n- [Q1] Do we add Redis for caching?\n"
    )
    (store_path / "state_history.md").write_text("## 2026-04-01T10:00Z\n\nEarlier note.\n\n---\n")
    for num, title, rationale, conf, iso in (
        (
            1,
            "Adopt Postgres",
            "ACID compliance trumps document flexibility for this workload.",
            DecisionConfidence.high,
            date(2026, 1, 1),
        ),
        (
            2,
            "Use FastAPI",
            "FastAPI plus Mangum is the Lambda deployment combination.",
            DecisionConfidence.medium,
            date(2026, 1, 2),
        ),
    ):
        body = format_decision(
            Decision(
                num=num,
                title=title,
                rationale=rationale,
                confidence=conf,
                status=DecisionStatus.active,
                date=iso,
            )
        )
        slug = title.lower().replace(" ", "-")
        (store_path / "decisions" / f"{num:03d}-{slug}.md").write_text(body)


def test_l0_content_matches_main_byte_for_byte(tmp_path):
    store_path = tmp_path / "store"
    _seed_baseline_store(store_path)
    envelope = tool_get_context(store_path, 0)
    assert envelope["content"] == _BASELINE_L0
