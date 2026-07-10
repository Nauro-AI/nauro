"""Tests for the auto-generated CLI commands.

The auto-generator walks ``nauro_core.mcp_tools.ALL_TOOLS`` and registers
one Typer command per read tool in the explicit allowlist. The tests
verify:

- The closed allowlist holds (every allowlisted read tool surfaces; no
  write tool ever surfaces, even after a future registry addition).
- Each auto-gen command dispatches through the matching ``tool_<name>``
  adapter, returns a JSON envelope on stdout, and exits non-zero with
  guidance on the documented error paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nauro_core.mcp_tools import ALL_TOOLS
from typer.testing import CliRunner

from nauro.cli.autogen import AUTOGEN_ALLOWLIST
from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.demo import create_demo_project
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config

runner = CliRunner()


# All registered (auto-gen + hand-written) commands on the top-level app.
def _registered_command_names() -> set[str]:
    return {cmd.name or cmd.callback.__name__ for cmd in app.registered_commands}


@pytest.fixture
def demo_repo(tmp_path: Path, monkeypatch) -> tuple[str, str, Path, Path]:
    """Local-mode demo project rooted at tmp_path/repo with cwd inside it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("demo-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "demo-project"})
    create_demo_project(store_path)
    monkeypatch.chdir(repo)
    return "demo-project", pid, store_path, repo


@pytest.fixture
def empty_repo(tmp_path: Path, monkeypatch) -> tuple[str, str, Path, Path]:
    """Registered v2 project with an empty store (no decisions/snapshots)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2("bare-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    save_repo_config(repo, {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "bare-project"})
    monkeypatch.chdir(repo)
    return "bare-project", pid, store_path, repo


# ── Coverage / allowlist guard ──────────────────────────────────────────────


class TestAutogenCoverage:
    def test_every_allowlisted_tool_is_registered(self) -> None:
        registered = _registered_command_names()
        for name in AUTOGEN_ALLOWLIST:
            kebab = name.replace("_", "-")
            assert kebab in registered, (
                f"Auto-gen allowlist references {name!r} but no '{kebab}' command is registered."
            )

    def test_write_tool_only_surfaces_when_explicitly_allowlisted(self) -> None:
        """A write tool added to ALL_TOOLS must NOT surface as an auto-gen
        CLI command unless explicitly added to AUTOGEN_ALLOWLIST.
        """
        registered = _registered_command_names()
        write_tools = {
            spec["name"] for spec in ALL_TOOLS if not spec["annotations"].get("readOnlyHint", False)
        }
        for name in write_tools:
            kebab = name.replace("_", "-")
            if name in AUTOGEN_ALLOWLIST:
                assert kebab in registered, (
                    f"Allowlisted write tool {name!r} did not surface as '{kebab}'."
                )
            else:
                assert kebab not in registered, (
                    f"Write tool {name!r} surfaced as '{kebab}' — the auto-gen "
                    "allowlist must stay closed."
                )

    def test_allowlist_matches_registry_minus_list_projects(self) -> None:
        """Every registry tool except ``list_projects`` must be in the
        allowlist. ``list_projects`` stays out because local installs
        auto-resolve to a single project.
        """
        registry_names = {spec["name"] for spec in ALL_TOOLS}
        expected = registry_names - {"list_projects"}
        assert AUTOGEN_ALLOWLIST == expected

    def test_confirm_decision_not_autogen_allowlisted(self) -> None:
        """Sentinel: confirm_decision was removed with the trust-model
        relocation. It must stay out of the auto-gen allowlist so a future
        reintroduction in the registry does not silently surface a CLI."""
        assert "confirm_decision" not in AUTOGEN_ALLOWLIST


# ── Per-tool happy + error paths ────────────────────────────────────────────


class TestGetContext:
    def test_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-context"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert "content" in envelope

    def test_level_l2(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-context", "--level", "L2"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"

    def test_no_project(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["get-context"])
        assert result.exit_code == 1
        assert "No project found" in result.output


class TestGetRawFile:
    def test_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-raw-file", "project.md"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert envelope.get("content")

    def test_path_traversal_rejected(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-raw-file", "../../etc/passwd"])
        assert result.exit_code == 1
        # Traversal rejection writes to stderr (envelope still printed to stdout).
        assert "Invalid path" in result.output


class TestListDecisions:
    def test_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["list-decisions"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert isinstance(envelope.get("decisions"), list)
        assert len(envelope["decisions"]) > 0

    def test_limit_flag(self, demo_repo) -> None:
        result = runner.invoke(app, ["list-decisions", "--limit", "2"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert len(envelope["decisions"]) == 2


class TestGetDecision:
    def test_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "1"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert "content" in envelope

    def test_missing_decision_exits_nonzero(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "9999"])
        assert result.exit_code == 1
        # Error envelope still on stdout; reason goes to stderr.
        envelope = json.loads(result.stdout)
        assert envelope.get("error")

    def test_mode_flag_advertises_header_and_full_default_full(self) -> None:
        result = runner.invoke(app, ["get-decision", "--help"])
        assert result.exit_code == 0, result.output
        assert "header" in result.output
        assert "full" in result.output
        assert "[default: full]" in result.output

    def test_mode_header_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "1", "--mode", "header"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert "content" in envelope

    def test_mode_full_matches_default(self, demo_repo) -> None:
        default = runner.invoke(app, ["get-decision", "1"])
        explicit_full = runner.invoke(app, ["get-decision", "1", "--mode", "full"])
        assert default.exit_code == 0
        assert explicit_full.exit_code == 0
        assert default.stdout == explicit_full.stdout

    def test_mode_header_envelope_is_more_compact(self, demo_repo) -> None:
        full = json.loads(runner.invoke(app, ["get-decision", "1", "--mode", "full"]).stdout)
        header = json.loads(runner.invoke(app, ["get-decision", "1", "--mode", "header"]).stdout)
        assert len(header["content"]) < len(full["content"])

    def test_mode_bogus_is_rejected(self, demo_repo) -> None:
        result = runner.invoke(app, ["get-decision", "1", "--mode", "bogus"])
        # An out-of-enum --mode is a usage error: exit 2 with the valid choices
        # named in the message, before the adapter produces a result.
        assert result.exit_code == 2, result.output
        assert "header" in result.output
        assert '"store"' not in result.stdout


class TestDiffSinceLastSession:
    def test_happy_path(self, demo_repo) -> None:
        # Demo project ships with a single snapshot — diff falls into the
        # "Not enough snapshots" envelope branch (success, no error field).
        result = runner.invoke(app, ["diff-since-last-session"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert "diff" in envelope


class TestSearchDecisions:
    def test_happy_path(self, demo_repo) -> None:
        result = runner.invoke(app, ["search-decisions", "budget"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert isinstance(envelope.get("results"), list)

    def test_empty_query_rejected(self, demo_repo) -> None:
        result = runner.invoke(app, ["search-decisions", ""])
        assert result.exit_code == 1


class TestCheckDecision:
    DEMO_PROMPT = "Store dollar amounts as decimal numbers"

    def test_demo_prompt_finds_integer_cents_decision(self, demo_repo) -> None:
        result = runner.invoke(app, ["check-decision", self.DEMO_PROMPT])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        titles = [d["title"] for d in envelope.get("related_decisions", [])]
        assert any("Amounts stored in integer cents, never floating point" in t for t in titles)

    def test_no_project(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["check-decision", self.DEMO_PROMPT])
        assert result.exit_code == 1
        assert "No project found" in result.output


# ── Help surface ────────────────────────────────────────────────────────────


class TestAutogenHelp:
    def test_command_help_is_exactly_the_first_description_paragraph(self) -> None:
        """Registered help must equal the tool description's first paragraph.

        The rest of the description is MCP-client prose (multi-paragraph
        agent guidance) and stays off the --help surface.
        """
        import click
        import typer as typer_mod

        command = typer_mod.main.get_command(app)
        ctx = click.Context(command, info_name="nauro")
        specs = {spec["name"]: spec for spec in ALL_TOOLS}
        for name in AUTOGEN_ALLOWLIST:
            sub = command.get_command(ctx, name.replace("_", "-"))
            assert sub is not None
            assert sub.help == specs[name]["description"].split("\n\n", 1)[0]

    def test_propose_decision_help_omits_later_paragraphs(self) -> None:
        """'human-in-the-loop' appears only in the description's second
        paragraph — a marker that later paragraphs never leak into --help
        output."""
        result = runner.invoke(app, ["propose-decision", "--help"])
        assert result.exit_code == 0, result.output
        assert "Record an architectural decision" in result.output
        assert "human-in-the-loop" not in result.output


# ── Cross-cutting invariants ────────────────────────────────────────────────


class TestAutogenInvariants:
    def test_json_output_is_default(self, demo_repo) -> None:
        result = runner.invoke(app, ["list-decisions"])
        assert result.exit_code == 0
        assert result.stdout.lstrip().startswith("{")

    def test_json_flag_is_noop(self, demo_repo) -> None:
        with_flag = runner.invoke(app, ["list-decisions", "--json"])
        without_flag = runner.invoke(app, ["list-decisions"])
        assert with_flag.exit_code == 0
        assert without_flag.exit_code == 0
        assert with_flag.stdout == without_flag.stdout

    def test_missing_store_returns_guidance(self, empty_repo) -> None:
        import shutil

        _name, _pid, store_path, _repo = empty_repo
        shutil.rmtree(store_path)
        result = runner.invoke(app, ["list-decisions"])
        assert result.exit_code == 1
        assert "Welcome to Nauro" in result.output
