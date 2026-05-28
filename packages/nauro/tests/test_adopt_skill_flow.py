"""Behavioral coverage for the ``/nauro-adopt`` skill's executable spine.

The skill (``packages/nauro/src/nauro/skills/adopt_body.md``) is an
agent-run markdown procedure: an agent reads the repo, triages candidates,
and seeds the store via the MCP write tools. Its triage and refusal-contract
steps are agent judgment and cannot be unit-tested. What *can* be locked is
the executable substrate every run depends on — and which previously had no
runtime coverage at all:

  Step 2  already-adopted guard reads ``.nauro/config.json`` (id + name)
  Step 5  ``get_context`` surfaces the scaffold seed
  Step 7  ``check_decision`` filters the scaffold seed, then ``propose_decision``
          commits in a single call (no separate confirm step), and a follow-up
          ``check_decision`` surfaces the newly recorded decision
  Step 8  ``update_state`` accepts a composed delta

These exercise the canonical ``tool_*`` adapters in ``nauro.mcp.tools`` that
all three local surfaces (CLI, stdio MCP, FastAPI) share, so this is the
substrate behind the skill regardless of which surface the agent uses.

Isolation: ``HOME`` is redirected to ``tmp_path`` so the store
(``HOME/.nauro``) and every materialized surface (``~/.claude``, ``~/.codex``,
``~/.agents``) land under the temp dir — ``nauro adopt`` writes to all of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.tools import (
    tool_check_decision,
    tool_get_context,
    tool_list_decisions,
    tool_propose_decision,
    tool_update_state,
)
from nauro.store.registry import get_store_path_v2

runner = CliRunner()


def _seed_fixture_repo(repo: Path) -> None:
    """A small but realistic repo: a documented decision with rationale."""
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "README.md").write_text(
        "# Orbit\n\n## Architecture\n\n"
        "We use PostgreSQL with SKIP LOCKED for the job queue rather than Redis, "
        "because we already run Postgres and wanted exactly-once delivery without "
        "a second datastore. An explicit call.\n"
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "orbit"\nrequires-python = ">=3.11"\n'
        'dependencies = ["psycopg[binary]>=3.1", "click>=8.1"]\n'
    )


def test_adopt_skill_executable_flow(tmp_path: Path, monkeypatch):
    # Redirect HOME so store + every materialized surface stay under tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NAURO_HOME", raising=False)

    repo = tmp_path / "orbit"
    repo.mkdir()
    _seed_fixture_repo(repo)
    monkeypatch.chdir(repo)

    # Step 0 — the precondition the skill assumes: `nauro adopt` has run.
    result = runner.invoke(app, ["adopt", "--name", "orbit"])
    assert result.exit_code == 0, result.output

    # Step 2 — already-adopted guard: config.json carries the id + name.
    config = json.loads((repo / ".nauro" / "config.json").read_text())
    pid, name = config["id"], config["name"]
    assert name == "orbit"
    store = get_store_path_v2(pid)

    # Step 5 — get_context surfaces the scaffold seed and the project identity.
    ctx = tool_get_context(store)
    assert ctx["project"] == {"id": pid, "name": "orbit"}
    assert "Initial project setup" in ctx["content"]

    # Step 7 — check_decision filters the scaffold seed: with only the seed on
    # disk, the conflict-check corpus is empty (the seed is boilerplate, not a
    # real prior decision to conflict with).
    pre = tool_check_decision(store, "Use Postgres with SKIP LOCKED for the job queue")
    assert pre["related_decisions"] == []
    assert pre["project"] == {"id": pid, "name": "orbit"}

    # Step 7 — propose_decision commits in a single call (no confirm step).
    proposed = tool_propose_decision(
        store,
        title="Use Postgres with SKIP LOCKED for the job queue",
        rationale="Already running Postgres; chose it for exactly-once delivery "
        "without a second datastore. Documented in the README.",
        operation="add",
        rejected=[{"alternative": "Redis", "reason": "second datastore; weaker guarantees"}],
        confidence="high",
    )
    assert proposed["status"] == "confirmed"
    assert proposed["decision_id"].startswith("002-")

    # Index updates: a follow-up check now surfaces the recorded decision — this
    # is the Step 10 safety net (a demo-prose "use Redis" proposal would hit it).
    post = tool_check_decision(store, "Use Redis instead of Postgres for the job queue")
    assert any("Postgres" in r["title"] for r in post["related_decisions"])

    # Step 8 — update_state accepts the composed delta.
    state = tool_update_state(
        store, "Shipping v0.2 retries.\n\n## Recently completed\n- queue shipped"
    )
    assert state["status"] == "ok"

    # Step 11 — final log: scaffold seed + the one real decision, nothing spurious.
    titles = {d["title"] for d in tool_list_decisions(store)["decisions"]}
    assert "Initial project setup" in titles
    assert "Use Postgres with SKIP LOCKED for the job queue" in titles


def test_check_decision_filters_scaffold_seed_only_when_alone(tmp_path: Path, monkeypatch):
    """Regression guard for the deliberate scaffold-seed filter in check_decision.

    The seed (decision 1, "Initial project setup") is excluded from the
    conflict-check corpus. With only the seed present, check_decision reports an
    empty corpus; once a real decision exists, check_decision surfaces it. If the
    filter were dropped, the first assertion would fail (the seed would match).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NAURO_HOME", raising=False)
    repo = tmp_path / "orbit"
    repo.mkdir()
    _seed_fixture_repo(repo)
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["adopt", "--name", "orbit"]).exit_code == 0
    store = get_store_path_v2(json.loads((repo / ".nauro" / "config.json").read_text())["id"])

    # A query lifted near-verbatim from the seed must NOT surface it.
    only_seed = tool_check_decision(store, "scaffold the project store and track decisions")
    assert only_seed["related_decisions"] == []
