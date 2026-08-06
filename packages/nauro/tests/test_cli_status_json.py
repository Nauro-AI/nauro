"""Tests for ``nauro status --json``.

The payload is a curated projection of one status run: capability facts
only, never the internal wiring state (recorded command strings, per-repo
hook tuples). The key set is pinned exactly so additions are deliberate.
Resolution failures keep the pinned exit 1 and stderr message and add a
structured error envelope on stdout using the resolution reason
vocabulary.
"""

from __future__ import annotations

import json
import shutil
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nauro.agents import AGENT_NAMES
from nauro.cli.integrations import codex_config
from nauro.cli.integrations.skills import OPT_IN_SKILL_NAMES, SKILL_NAMES
from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _setup_project(tmp_path, monkeypatch, name="testproj"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2(name, [repo])
    scaffold_project_store(name, store)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        codex_config, "codex_config_path", lambda: tmp_path / "codex-home" / "config.toml"
    )
    return store, repo


def _wire_repo_mcp(repo):
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"nauro": {"command": "nauro"}}}))


def _wire_codex_hooks(repo, *, command="nauro", events=("SessionStart", "SubagentStart")):
    entry = {
        "type": "command",
        "command": f"test -x {command} || exit 0; exec {command} hook codex-bootstrap",
    }
    hooks = {event: [{"hooks": [entry]}] for event in events}
    path = repo / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))


def _absent_counts(expected: int) -> dict:
    """Counts for a bundled artifact set that is not installed at all."""
    return {"present": 0, "current": 0, "expected": expected}


def test_status_json_happy_path_golden_payload(tmp_path, monkeypatch):
    """Full golden comparison of the payload on a fresh local project.

    Pins the exact key set and every value at once — an added, removed, or
    silently changed field fails here, not just a key-set drift. The
    isolated HOME has no skills or agents installed, nothing wired, and no
    cloud sync, so every fact is deterministic.
    """
    store, _repo = _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload == {
        "project": "testproj",
        "project_id": store.name,
        "store_path": str(store),
        "shared_name_count": 0,
        "sync": {"cloud": False, "authenticated": False, "active": False},
        # Nothing wired means nothing to probe: probed False, healthy null.
        "mcp": {
            "repo_count": 1,
            "wired_repos": 0,
            "codex_global": False,
            "probed": False,
            "healthy": None,
        },
        # No hooks configured: completeness is not applicable.
        "codex_hooks": {
            "repo_count": 1,
            "configured_repos": 0,
            "complete": None,
            "probed": False,
            "healthy": None,
        },
        "skills": {
            "core": {
                "claude": _absent_counts(len(SKILL_NAMES)),
                "codex": _absent_counts(len(SKILL_NAMES)),
            },
            "opt_in": {
                "claude": _absent_counts(len(OPT_IN_SKILL_NAMES)),
                "codex": _absent_counts(len(OPT_IN_SKILL_NAMES)),
            },
            "legacy_codex_copies": 0,
        },
        "workflow_agents": {
            "claude": _absent_counts(len(AGENT_NAMES)),
            "codex": _absent_counts(len(AGENT_NAMES)),
        },
        "agents_md": {"repo_count": 1, "generated_repos": 0},
        # Local-only project: no remote comparison. The scaffold seeds one
        # decision.
        "decisions": {"local": 1, "remote": None, "in_sync": None, "last_full_sync": None},
    }


def test_status_json_probed_and_healthy_when_wired(tmp_path, monkeypatch):
    _store, repo = _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(repo)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["mcp"]["repo_count"] == 1
    assert payload["mcp"]["wired_repos"] == 1
    # The conftest probe stub reports every recorded command healthy.
    assert payload["mcp"]["probed"] is True
    assert payload["mcp"]["healthy"] is True


def test_status_json_no_probe_yields_null_health(tmp_path, monkeypatch):
    _store, repo = _setup_project(tmp_path, monkeypatch)
    _wire_repo_mcp(repo)

    result = runner.invoke(app, ["status", "--json", "--no-probe"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["mcp"]["wired_repos"] == 1
    assert payload["mcp"]["probed"] is False
    assert payload["mcp"]["healthy"] is None
    assert payload["codex_hooks"]["probed"] is False
    assert payload["codex_hooks"]["healthy"] is None


def test_status_json_hooks_complete_when_configured(tmp_path, monkeypatch):
    """With hooks configured, complete carries the real completeness bool.

    Completeness is null only when nothing is configured; a fully wired
    lifecycle reports True.
    """
    _store, repo = _setup_project(tmp_path, monkeypatch)
    _wire_codex_hooks(repo)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["codex_hooks"]["configured_repos"] == 1
    assert payload["codex_hooks"]["complete"] is True


def test_status_json_no_project_error_envelope(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 1

    payload = json.loads(result.stdout)
    assert set(payload) == {"status", "error"}
    assert payload["status"] == "error"
    assert set(payload["error"]) == {"reason", "guidance"}
    assert payload["error"]["reason"] == "no_project"
    assert payload["error"]["guidance"].startswith("No project found for current directory.")
    # The pinned stderr message is unchanged.
    assert "No project found. Run 'nauro init <name>' to get started." in result.stderr


def test_status_json_unknown_project_error_envelope(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status", "--project", "nope", "--json"])
    assert result.exit_code == 1

    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["reason"] == "unknown_project"
    assert payload["error"]["guidance"].startswith("Unknown project 'nope'.")


def test_status_json_ambiguous_project_error_envelope(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    register_project_v2("dup", [tmp_path / "a"])
    register_project_v2("dup", [tmp_path / "b"])

    result = runner.invoke(app, ["status", "--project", "dup", "--json"])
    assert result.exit_code == 1

    payload = json.loads(result.stdout)
    assert payload["error"]["reason"] == "ambiguous_project"
    assert payload["error"]["guidance"].startswith("Multiple projects named 'dup'.")


def test_status_json_disconnected_project_error_envelope(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    _pid, gone_store = register_project_v2("gone", [tmp_path / "elsewhere"])
    shutil.rmtree(gone_store)

    result = runner.invoke(app, ["status", "--project", "gone", "--json"])
    assert result.exit_code == 1

    payload = json.loads(result.stdout)
    assert payload["error"]["reason"] == "disconnected_project"
    assert "nauro reconnect" in payload["error"]["guidance"]
    # Disconnected keeps its typed guidance; the generic no-project line stays out.
    assert "No project found. Run 'nauro init <name>'" not in result.stderr


def _setup_cloud_project(tmp_path, monkeypatch):
    """Authed cloud-mode project, so the sync-state read path is active."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2(
        "cloudproj",
        [repo],
        mode=REPO_CONFIG_MODE_CLOUD,
        project_id="01KQ6AZGNA0B3QBF67NBXP3S45",
        server_url="https://example.test",
    )
    scaffold_project_store("cloudproj", store)
    save_config({"auth": {"access_token": "tok", "sub": "x"}})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        codex_config, "codex_config_path", lambda: tmp_path / "codex-home" / "config.toml"
    )
    return store


def test_status_survives_wrong_shape_sync_state(tmp_path, monkeypatch):
    """A broken .sync-state.json never crashes status.

    load_state only guards JSON decode errors — valid JSON of the wrong
    shape raises AttributeError. With sync active and the remote count
    unavailable, the pre-existing behavior was exit 0 without touching the
    sync-state file; the collect pass now reads it, so the read must
    degrade to "no recorded sync" in both output modes.
    """
    store = _setup_cloud_project(tmp_path, monkeypatch)
    (store / ".sync-state.json").write_text(json.dumps(["wrong", "shape"]))

    with patch("nauro.cli.commands.status._count_remote_decisions", return_value=None):
        human = runner.invoke(app, ["status"])
        as_json = runner.invoke(app, ["status", "--json"])

    assert human.exit_code == 0, human.output
    assert "could not reach remote" in human.output
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["decisions"]["remote"] is None
    assert payload["decisions"]["last_full_sync"] is None


@pytest.mark.parametrize("bad_value", [12345, ["not", "a", "string"]], ids=["int", "list"])
def test_status_survives_non_string_last_full_sync(tmp_path, monkeypatch, bad_value):
    """A parseable state file with a non-string last_full_sync never crashes.

    load_state passes the value through untyped, so it crosses the
    exception guard; without the isinstance sanitization the JSON path dies
    on payload validation and the human path on rendering the timestamp.
    The remote count is reachable here so the human path actually reaches
    its last-sync branch.
    """
    store = _setup_cloud_project(tmp_path, monkeypatch)
    (store / ".sync-state.json").write_text(json.dumps({"files": {}, "last_full_sync": bad_value}))

    with patch("nauro.cli.commands.status._count_remote_decisions", return_value=1):
        human = runner.invoke(app, ["status"])
        as_json = runner.invoke(app, ["status", "--json"])

    assert human.exit_code == 0, human.output
    assert "Decisions: 1 local, 1 remote (in sync)" in human.output
    assert "Last sync:" not in human.output
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["decisions"]["remote"] == 1
    assert payload["decisions"]["last_full_sync"] is None
