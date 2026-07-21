"""Cross-surface wiring for write-path provenance.

Asserts each transport stamps the right origin (stdio-MCP reads the injected
Context's clientInfo; the CLI stamps ``transport: cli``), that the injected
Context parameter stays out of the derived tool schema, and that adding
provenance capture added no new tool or CLI surface.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nauro.cli.autogen import AUTOGEN_ALLOWLIST
from nauro.cli.main import app
from nauro.mcp import stdio_server, tools
from nauro.mcp.stdio_server import mcp, propose_decision
from nauro.store.journal import read_events
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

_LONG_RATIONALE = "Adopt the widget subsystem because it is measurably better here. " * 3

_WRITE_TOOLS = ("propose_decision", "flag_question", "update_state")


def _fake_ctx(name: str | None, version: str | None) -> SimpleNamespace:
    """A stand-in FastMCP Context exposing session.client_params.clientInfo."""
    client_info = SimpleNamespace(name=name, version=version)
    client_params = SimpleNamespace(clientInfo=client_info)
    session = SimpleNamespace(client_params=client_params)
    return SimpleNamespace(session=session)


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(tools, "_try_push", lambda _store_path: None)
    _pid, store_path = register_project_v2("provproj", [tmp_path / "repo"])
    scaffold_project_store("provproj", store_path)
    return store_path


# --- stdio-MCP origin --------------------------------------------------------


class TestStdioOrigin:
    def test_client_info_recorded_and_sanitized(self, store: Path):
        ctx = _fake_ctx("Claude\x00 Code", "1." + "9" * 400)
        result = propose_decision(
            project_id="provproj",
            title="Adopt widgets",
            rationale=_LONG_RATIONALE,
            mcp_ctx=ctx,
        )
        assert result["status"] == "confirmed"
        events = read_events(store)
        assert len(events) == 1
        origin = events[0].origin
        assert origin is not None
        assert origin.transport == "stdio-mcp"
        assert origin.client_name == "Claude Code"  # NUL stripped
        assert len(origin.client_version) == 256  # length bounded

    def test_none_safe_when_client_params_absent(self, store: Path):
        # A Context with no client_params, and no Context at all, both yield a
        # well-formed stdio-mcp descriptor with the name/version unset.
        for ctx in (SimpleNamespace(session=SimpleNamespace(client_params=None)), None):
            origin = stdio_server._origin_from_ctx(ctx)
            assert origin.transport == "stdio-mcp"
            assert origin.client_name is None
            assert origin.client_version is None


# --- schema invariance -------------------------------------------------------


class TestSchemaInvariance:
    @pytest.fixture
    def tools_by_name(self):
        return {t.name: t for t in mcp._tool_manager.list_tools()}

    @pytest.mark.parametrize("name", _WRITE_TOOLS)
    def test_mcp_ctx_absent_from_schema(self, tools_by_name, name):
        props = tools_by_name[name].parameters.get("properties", {})
        assert "mcp_ctx" not in props

    def test_flag_question_still_advertises_context(self, tools_by_name):
        props = tools_by_name["flag_question"].parameters["properties"]
        assert "context" in props


# --- CLI attribution ---------------------------------------------------------


class TestCliAttribution:
    def _project(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _pid, store = register_project_v2("cliproj", [repo])
        scaffold_project_store("cliproj", store)
        monkeypatch.chdir(repo)
        return repo, store

    def test_autogen_propose_decision_stamps_cli(self, tmp_path: Path, monkeypatch):
        _repo, store = self._project(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["propose-decision", _LONG_RATIONALE, "--title", "Adopt widgets"],
        )
        assert result.exit_code == 0, result.output
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "propose_decision"
        assert events[0].origin is not None
        assert events[0].origin.transport == "cli"
        assert events[0].origin.client_name == "nauro-cli"

    def test_note_stamps_cli(self, tmp_path: Path, monkeypatch):
        _repo, store = self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["note", "Use Postgres for v2 storage"])
        assert result.exit_code == 0, result.output
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "propose_decision"
        assert events[0].origin.transport == "cli"
        assert events[0].origin.client_name == "nauro-cli"

    def test_import_adr_stamps_cli(self, tmp_path: Path, monkeypatch):
        _repo, store = self._project(tmp_path, monkeypatch)
        adr_dir = tmp_path / "adrs"
        adr_dir.mkdir()
        (adr_dir / "001-use-postgres.md").write_text(
            "# Use Postgres\n\n## Context\n\nWe need a relational store.\n"
        )
        result = runner.invoke(app, ["import", "--adr", str(adr_dir)])
        assert result.exit_code == 0, result.output
        events = read_events(store)
        assert len(events) == 1
        assert events[0].operation == "propose_decision"
        assert events[0].origin.transport == "cli"
        assert events[0].origin.client_name == "nauro-cli"


# --- no new surface ----------------------------------------------------------


class TestNoNewSurface:
    def test_ten_tools_still_registered(self):
        assert len(mcp._tool_manager.list_tools()) == 10

    def test_autogen_allowlist_unchanged(self):
        assert AUTOGEN_ALLOWLIST == frozenset(
            {
                "get_context",
                "get_raw_file",
                "list_decisions",
                "get_decision",
                "diff_since_last_session",
                "search_decisions",
                "check_decision",
                "update_state",
                "flag_question",
                "propose_decision",
            }
        )
