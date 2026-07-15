"""Characterization pins for the ``nauro setup`` command surface.

Pins the observable behavior of ``setup claude-code``, ``setup cursor``,
``setup codex``, and ``setup all`` ahead of the internal restructuring of
the setup module, so move-only changes can be verified behavior-neutral.
Transcripts are pinned as they stand today, including asymmetries (e.g.
``.mcp.json`` re-reports "wrote" on a no-op re-run while codex and hooks
report no-op statuses); a failure here after a refactor means observable
behavior changed, not that this file is wrong.

Rules for this module:

- Every test drives the CLI through ``CliRunner`` on ``nauro.cli.main.app``.
- Expected strings are inline literals; nothing is imported from
  ``nauro.cli.commands.setup`` so a refactor cannot rewrite the expectations
  it is being checked against.
- Projects are registered via ``register_project_v2`` without ``git init``
  so git-hygiene notes stay out of the transcripts.
- stdout and stderr are asserted separately, plus the exact exit code.
- Volatile values are masked via ``normalize_transcript``: the pytest tmp
  dir as ``{TMP}`` and the resolved nauro entrypoint as ``{NAURO_CMD}``.

Seam note: the resolver-warning tests override the conftest probe seam
(``cli_utils.probe_nauro_command`` / ``cli_utils._is_durable_install_path``)
patched by the autouse ``_neutralize_nauro_command_probe`` fixture. They ride
that fixture's existing retarget obligation: if the seam moves, retarget
these overrides together with the fixture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import normalize_transcript, snapshot_tree

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-shaped transcripts, env-based home redirection, symlink fixtures",
)

runner = CliRunner()


# ─── shared notice lines (inline literals, one place per repeated line) ──────

TRY_IT_LINE = 'Try it now from this shell: nauro check-decision "<approach>"\n'

CLAUDE_NEXT_LINE = (
    "Next: start a Claude Code session in one of the repos."
    " The MCP server will start automatically.\n"
)

CURSOR_NEXT_LINE = (
    "Next: open this repo in Cursor and start a chat \u2014 Nauro MCP will connect.\n"
)

CODEX_NEXT_LINE = "Next: run a Codex session \u2014 it reads ~/.codex/config.toml on start.\n"

ALL_RESTART_LINE = (
    "Next: start a fresh agent session (Claude Code/Cursor) \u2014 MCP config is read at"
    " session start.\n"
)

HOOKS_NOTICE_LINE = (
    "The advisory hook surfaces related decisions as context on each turn (BM25 retrieval)"
    " and never blocks. Start a new Claude Code session in a wired repo for it to take"
    " effect.\n"
)

CODEX_HOOKS_NOTICE_LINE = (
    "Codex skips new or changed hooks until you review and trust them. Start Codex in a"
    " wired repo, then open `/hooks` to review the project hooks.\n"
)

CONNECTOR_NOTICE_LINE = (
    "Cloud users: name the remote MCP connector exactly `Nauro` so the bundled @nauro-*"
    " subagents' `mcp__claude_ai_Nauro__*` tools resolve.\n"
)

SKILLS_NEEDS_SUBAGENTS_LINE = (
    "nauro-ship-task references the bundled @nauro-* subagents (and nauro-loop dispatches"
    " that chain); pass `--with-subagents` to install them too.\n"
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Point the home directory at ``tmp_path`` for every test.

    User-scope sinks (``~/.claude/skills``, ``~/.agents/skills``,
    ``~/.codex/config.toml``, ``~/.claude.json``, ``~/.claude/agents``) must
    land inside the test tree. Both HOME and USERPROFILE are redirected,
    mirroring test_setup_atomic_writes.py.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _register_project(tmp_path: Path, monkeypatch, name="proj", repos=("repo",)):
    """Register a v2 project without ``git init`` and chdir into its first repo.

    No git repo means ``public_surface_git_warnings`` stays silent, keeping
    the pinned transcripts free of environment-dependent hygiene notes.
    """
    paths = []
    for rel in repos:
        p = tmp_path / rel
        p.mkdir()
        paths.append(p)
    pid, store = register_project_v2(name, paths)
    scaffold_project_store(name, store)
    for p in paths:
        save_repo_config(p, {"mode": "local", "id": pid, "name": name})
    monkeypatch.chdir(paths[0])
    return pid, store, paths


def _norm(text: str, tmp_path: Path) -> str:
    return normalize_transcript(text, {str(tmp_path): "{TMP}"})


def _all_add_plain_expected() -> str:
    """The ``setup all`` add transcript for a single-repo project, no flags."""
    return (
        "Configured Nauro for project 'proj' across all surfaces:\n"
        "\n"
        "  {TMP}/repo: wrote nauro to .mcp.json\n"
        "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
        "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
        "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
        "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
        "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
        "  {TMP}/repo: regenerated AGENTS.md\n"
        "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
    )


# ─── transcript pins ─────────────────────────────────────────────────────────


class TestClaudeCodeTranscripts:
    def test_add(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "claude-code"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj':\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "\n"
            "AGENTS.md:\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + CLAUDE_NEXT_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_add_with_hooks(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "claude-code", "--with-hooks"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj':\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "\n"
            "Hooks:\n"
            "  {TMP}/repo: wrote nauro hook to .claude/settings.json\n"
            "\n"
            "AGENTS.md:\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + CLAUDE_NEXT_LINE + "\n" + HOOKS_NOTICE_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_remove_after_plain_add(self, tmp_path: Path, monkeypatch):
        """The remove path emits a Hooks section even when no hook was wired."""
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "claude-code"]).exit_code == 0

        result = runner.invoke(app, ["setup", "claude-code", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro for project 'proj':\n"
            "\n"
            "  {TMP}/repo: removed nauro from .mcp.json\n"
            "\n"
            "Hooks:\n"
            "  {TMP}/repo: no nauro hook to remove\n"
        )
        assert result.stderr == ""


class TestCursorTranscripts:
    def test_add(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "cursor"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro (Cursor) for project 'proj':\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "\n" + CURSOR_NEXT_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_remove_after_add(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "cursor"]).exit_code == 0

        result = runner.invoke(app, ["setup", "cursor", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro (Cursor) for project 'proj':\n"
            "\n"
            "  {TMP}/repo: removed nauro from .cursor/mcp.json\n"
        )
        assert result.stderr == ""


class TestCodexTranscripts:
    def test_add(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "codex"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "\n" + CODEX_NEXT_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_add_with_hooks(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "codex", "--with-hooks"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "\n"
            "Hooks:\n"
            "  {TMP}/repo: wrote nauro hooks to .codex/hooks.json\n"
            "\n" + CODEX_NEXT_LINE + "\n" + CODEX_HOOKS_NOTICE_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_remove_with_project_remaining_preserves_entry(self, tmp_path: Path, monkeypatch):
        """With a project still registered, the user-global entry is preserved."""
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "codex", "--with-hooks"]).exit_code == 0

        result = runner.invoke(app, ["setup", "codex", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Codex: preserved nauro entry in {TMP}/.codex/config.toml (1 nauro project"
            " registered; run 'nauro setup all --remove' on the last project to clear this"
            " user-global entry)\n"
            "\n"
            "Hooks:\n"
            "  {TMP}/repo: removed nauro hooks from .codex/hooks.json\n"
        )
        assert result.stderr == ""
        assert (tmp_path / ".codex" / "config.toml").is_file()

    def test_remove_with_empty_registry_clears_entry(self, tmp_path: Path, monkeypatch):
        """No registered projects: the entry is cleared and hook cleanup punts."""
        add = runner.invoke(app, ["setup", "codex"])
        assert add.exit_code == 0

        result = runner.invoke(app, ["setup", "codex", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Codex: removed nauro from {TMP}/.codex/config.toml\n"
            "\n"
            "Hooks:\n"
            "  Project-scoped Codex hooks were not removed because no Nauro project resolves"
            " from this directory. Run this command from each wired repo to remove them.\n"
        )
        assert result.stderr == ""


class TestSetupAllTranscripts:
    def test_add_plain(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == _all_add_plain_expected()
        assert result.stderr == ""

    def test_add_full_flags(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(
            app, ["setup", "all", "--with-skills", "--with-subagents", "--with-hooks"]
        )

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-loop/SKILL.md\n"
            "  installed {TMP}/.claude/agents/nauro-planner.md\n"
            "  installed {TMP}/.claude/agents/nauro-executor.md\n"
            "  installed {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  installed {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: wrote nauro hook to .claude/settings.json\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-ship-task.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-context.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-loop.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-loop/SKILL.md\n"
            "  {TMP}/repo: wrote nauro hooks to .codex/hooks.json\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n"
            + CONNECTOR_NOTICE_LINE
            + "\n"
            + HOOKS_NOTICE_LINE
            + "\n"
            + CODEX_HOOKS_NOTICE_LINE
            + "\n"
            + ALL_RESTART_LINE
            + "\n"
            + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_remove_as_last_project(self, tmp_path: Path, monkeypatch):
        """Last project on the machine: user-scope artifacts are cleared too."""
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "all"]).exit_code == 0

        result = runner.invoke(app, ["setup", "all", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: removed nauro from .mcp.json\n"
            "  removed {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: no nauro hook to remove\n"
            "  {TMP}/repo: removed nauro from .cursor/mcp.json\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: removed nauro from {TMP}/.codex/config.toml\n"
            "  removed {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: no nauro Codex hooks to remove\n"
            "  {TMP}/repo: removed generated AGENTS.md\n"
        )
        assert result.stderr == ""

    def test_remove_with_other_project_registered(self, tmp_path: Path, monkeypatch):
        """Shared user-scope artifacts are preserved while another project remains."""
        _register_project(tmp_path, monkeypatch)
        other = tmp_path / "other"
        other.mkdir()
        register_project_v2("other", [other])
        assert runner.invoke(app, ["setup", "all"]).exit_code == 0

        result = runner.invoke(app, ["setup", "all", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: removed nauro from .mcp.json\n"
            "  preserved ~/.claude/skills/nauro-* (other nauro projects still registered)\n"
            "  {TMP}/repo: no nauro hook to remove\n"
            "  {TMP}/repo: removed nauro from .cursor/mcp.json\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: preserved nauro entry in {TMP}/.codex/config.toml (other nauro projects"
            " still registered)\n"
            "  preserved ~/.agents/skills/nauro-* (other nauro projects still registered)\n"
            "  {TMP}/repo: no nauro Codex hooks to remove\n"
            "  {TMP}/repo: removed generated AGENTS.md\n"
        )
        assert result.stderr == ""
        assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()
        assert (tmp_path / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").is_file()

    def test_add_multi_repo_interleaving(self, tmp_path: Path, monkeypatch):
        """Per-repo ordering: all .mcp.json writes first, then per-repo Cursor pairs."""
        _register_project(tmp_path, monkeypatch, repos=("repo1", "repo2"))

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo1: wrote nauro to .mcp.json\n"
            "  {TMP}/repo2: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo1: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo1/.cursor/rules/nauro-adopt.mdc\n"
            "  {TMP}/repo2: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo2/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo1: regenerated AGENTS.md\n"
            "  {TMP}/repo2: regenerated AGENTS.md\n"
            "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_add_with_skills_only(self, tmp_path: Path, monkeypatch):
        """--with-skills alone installs opt-in skills but no subagents or hooks.

        Opt-in skills land in both skill roots with their Cursor rules; no agents
        are installed, no hooks wired, and the transcript warns that the ship-task
        chain still needs --with-subagents.
        """
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all", "--with-skills"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-loop/SKILL.md\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-ship-task.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-context.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-loop.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-loop/SKILL.md\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + SKILLS_NEEDS_SUBAGENTS_LINE + "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_add_with_subagents_only(self, tmp_path: Path, monkeypatch):
        """--with-subagents alone installs the four agents but no opt-in skills.

        Only the adopt skill and rule are written, no hooks are wired, and the
        transcript emits the connector-naming notice.
        """
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all", "--with-subagents"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  installed {TMP}/.claude/agents/nauro-planner.md\n"
            "  installed {TMP}/.claude/agents/nauro-executor.md\n"
            "  installed {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  installed {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + CONNECTOR_NOTICE_LINE + "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_add_with_hooks_only(self, tmp_path: Path, monkeypatch):
        """--with-hooks alone wires the Claude and Codex hooks but no subagents.

        Only the adopt skill and rule are written, and the transcript emits both
        hook notices.
        """
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all", "--with-hooks"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: wrote nauro hook to .claude/settings.json\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: wrote nauro hooks to .codex/hooks.json\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n"
            + HOOKS_NOTICE_LINE
            + "\n"
            + CODEX_HOOKS_NOTICE_LINE
            + "\n"
            + ALL_RESTART_LINE
            + "\n"
            + TRY_IT_LINE
        )
        assert result.stderr == ""

    def test_subagents_rerun_refresh_saves_bak(self, tmp_path: Path, monkeypatch):
        """Re-running --with-subagents over a user-edited agent stashes a .bak.

        Without --force-overwrite the differing file is updated and its prior
        content saved to <name>.md.bak; the unchanged agents report "unchanged".
        """
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "all", "--with-subagents"]).exit_code == 0
        (tmp_path / ".claude" / "agents" / "nauro-planner.md").write_text(
            "USER EDIT\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["setup", "all", "--with-subagents"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  updated {TMP}/.claude/agents/nauro-planner.md"
            " (previous saved to nauro-planner.md.bak)\n"
            "  unchanged {TMP}/.claude/agents/nauro-executor.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: nauro already configured in {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + CONNECTOR_NOTICE_LINE + "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""
        assert (tmp_path / ".claude" / "agents" / "nauro-planner.md.bak").read_text(
            encoding="utf-8"
        ) == "USER EDIT\n"

    def test_subagents_rerun_force_overwrite_no_bak(self, tmp_path: Path, monkeypatch):
        """--force-overwrite overwrites a user-edited agent in place with no .bak.

        Re-running --with-subagents --force-overwrite replaces the differing file
        directly and leaves no .bak sibling behind.
        """
        _register_project(tmp_path, monkeypatch)
        assert runner.invoke(app, ["setup", "all", "--with-subagents"]).exit_code == 0
        (tmp_path / ".claude" / "agents" / "nauro-planner.md").write_text(
            "USER EDIT\n", encoding="utf-8"
        )

        result = runner.invoke(app, ["setup", "all", "--with-subagents", "--force-overwrite"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  overwrote {TMP}/.claude/agents/nauro-planner.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-executor.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: nauro already configured in {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + CONNECTOR_NOTICE_LINE + "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""
        assert not (tmp_path / ".claude" / "agents" / "nauro-planner.md.bak").exists()

    def test_remove_orphans_optin_skills_and_subagents(self, tmp_path: Path, monkeypatch):
        """Plain --remove orphans opt-in artifacts because it drops the flags.

        Not re-passing the opt-in flags means teardown strips only the
        always-installed adopt skill and rule; the opt-in skills (both skill
        roots), their Cursor rules, and the four subagents are all left behind,
        and the remove transcript is identical to a plain-add teardown. Changing
        this is a named follow-up, not a silent move-time delta;
        test_remove_with_flags_clears_optin_artifacts pins the complementary path
        where the flags are re-passed.
        """
        _register_project(tmp_path, monkeypatch)
        assert (
            runner.invoke(app, ["setup", "all", "--with-skills", "--with-subagents"]).exit_code == 0
        )

        result = runner.invoke(app, ["setup", "all", "--remove"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: removed nauro from .mcp.json\n"
            "  removed {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: no nauro hook to remove\n"
            "  {TMP}/repo: removed nauro from .cursor/mcp.json\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: removed nauro from {TMP}/.codex/config.toml\n"
            "  removed {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: no nauro Codex hooks to remove\n"
            "  {TMP}/repo: removed generated AGENTS.md\n"
        )
        assert result.stderr == ""
        # The opt-in artifacts survive plain --remove (orphaned, by current design):
        # opt-in skills in both skill roots, their Cursor rules, and all subagents.
        for root in (".claude/skills", ".agents/skills"):
            names = {p.name for p in (tmp_path / root).iterdir()}
            assert names == {"nauro-ship-task", "nauro-context", "nauro-loop"}
        rule_names = {p.name for p in (tmp_path / "repo" / ".cursor" / "rules").iterdir()}
        assert rule_names == {
            "nauro-ship-task.mdc",
            "nauro-context.mdc",
            "nauro-loop.mdc",
        }
        agent_names = {p.name for p in (tmp_path / ".claude" / "agents").iterdir()}
        assert agent_names == {
            "nauro-planner.md",
            "nauro-executor.md",
            "nauro-reviewer.md",
            "nauro-tech-lead.md",
        }

    def test_remove_with_flags_clears_optin_artifacts(self, tmp_path: Path, monkeypatch):
        """Re-passing the opt-in flags on remove clears every opt-in artifact.

        Complement to the orphan case: --remove --with-skills --with-subagents
        strips the opt-in skills (both roots), their Cursor rules, and the four
        subagents alongside the always-installed adopt artifacts, leaving no
        opt-in residue.
        """
        _register_project(tmp_path, monkeypatch)
        assert (
            runner.invoke(app, ["setup", "all", "--with-skills", "--with-subagents"]).exit_code == 0
        )

        result = runner.invoke(
            app, ["setup", "all", "--remove", "--with-skills", "--with-subagents"]
        )

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Removed Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: removed nauro from .mcp.json\n"
            "  removed {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  removed {TMP}/.claude/skills/nauro-ship-task/SKILL.md\n"
            "  removed {TMP}/.claude/skills/nauro-context/SKILL.md\n"
            "  removed {TMP}/.claude/skills/nauro-loop/SKILL.md\n"
            "  removed {TMP}/.claude/agents/nauro-planner.md\n"
            "  removed {TMP}/.claude/agents/nauro-executor.md\n"
            "  removed {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  removed {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: no nauro hook to remove\n"
            "  {TMP}/repo: removed nauro from .cursor/mcp.json\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-ship-task.mdc\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-context.mdc\n"
            "  removed {TMP}/repo/.cursor/rules/nauro-loop.mdc\n"
            "Codex: removed nauro from {TMP}/.codex/config.toml\n"
            "  removed {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  removed {TMP}/.agents/skills/nauro-ship-task/SKILL.md\n"
            "  removed {TMP}/.agents/skills/nauro-context/SKILL.md\n"
            "  removed {TMP}/.agents/skills/nauro-loop/SKILL.md\n"
            "  {TMP}/repo: no nauro Codex hooks to remove\n"
            "  {TMP}/repo: removed generated AGENTS.md\n"
        )
        assert result.stderr == ""
        # No opt-in residue: skill roots gone, cursor rules dir emptied, no agents.
        for root in (".claude/skills", ".agents/skills"):
            d = tmp_path / root
            assert not d.exists() or not any(d.iterdir())
        rules_dir = tmp_path / "repo" / ".cursor" / "rules"
        assert not rules_dir.exists() or not any(rules_dir.iterdir())
        agents_dir = tmp_path / ".claude" / "agents"
        assert not agents_dir.exists() or not any(agents_dir.iterdir())


# ─── command-level idempotency ───────────────────────────────────────────────


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in snapshot_tree(root)}


_AGENTS_TS_PREFIX = "<!-- Auto-generated by Nauro ("


def _mask_agents_md_timestamp(data: bytes) -> bytes:
    """Mask the single UTC-timestamp comment line in an AGENTS.md body."""
    lines = data.decode("utf-8").splitlines(keepends=True)
    masked = [
        "<!-- Auto-generated by Nauro ({TS}). -->\n" if line.startswith(_AGENTS_TS_PREFIX) else line
        for line in lines
    ]
    return "".join(masked).encode("utf-8")


def _assert_trees_identical(first: dict[str, bytes], second: dict[str, bytes]) -> None:
    """Byte-identity across runs; AGENTS.md compared with its timestamp masked.

    AGENTS.md is regenerated on every add run with a fresh UTC timestamp, so
    it is the one artifact excluded from byte-equality.
    """
    assert sorted(first) == sorted(second)
    for rel, before in first.items():
        after = second[rel]
        if rel.rsplit("/", 1)[-1] == "AGENTS.md":
            assert _mask_agents_md_timestamp(before) == _mask_agents_md_timestamp(after), rel
        else:
            assert before == after, rel


class TestCommandIdempotency:
    def test_claude_code_rerun(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        first = runner.invoke(app, ["setup", "claude-code"])
        assert first.exit_code == 0
        tree_first = _tree_bytes(tmp_path)

        second = runner.invoke(app, ["setup", "claude-code"])
        assert second.exit_code == 0

        _assert_trees_identical(tree_first, _tree_bytes(tmp_path))
        # The second run re-reports "wrote"/"regenerated" rather than a no-op.
        assert second.stdout == first.stdout
        assert second.stderr == ""

    def test_cursor_rerun(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        first = runner.invoke(app, ["setup", "cursor"])
        assert first.exit_code == 0
        tree_first = _tree_bytes(tmp_path)

        second = runner.invoke(app, ["setup", "cursor"])
        assert second.exit_code == 0

        _assert_trees_identical(tree_first, _tree_bytes(tmp_path))
        # The second run re-reports "wrote" rather than a no-op.
        assert second.stdout == first.stdout
        assert second.stderr == ""

    def test_codex_rerun(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)

        first = runner.invoke(app, ["setup", "codex"])
        assert first.exit_code == 0
        tree_first = _tree_bytes(tmp_path)

        second = runner.invoke(app, ["setup", "codex"])
        assert second.exit_code == 0

        _assert_trees_identical(tree_first, _tree_bytes(tmp_path))
        # Unlike the JSON MCP sinks, codex reports an explicit no-op.
        assert _norm(second.stdout, tmp_path) == (
            "Codex: nauro already configured in {TMP}/.codex/config.toml\n"
            "\n" + CODEX_NEXT_LINE + "\n" + TRY_IT_LINE
        )
        assert second.stderr == ""

    def test_all_full_flags_rerun(self, tmp_path: Path, monkeypatch):
        _register_project(tmp_path, monkeypatch)
        args = ["setup", "all", "--with-skills", "--with-subagents", "--with-hooks"]

        first = runner.invoke(app, args)
        assert first.exit_code == 0
        tree_first = _tree_bytes(tmp_path)

        second = runner.invoke(app, args)
        assert second.exit_code == 0

        _assert_trees_identical(tree_first, _tree_bytes(tmp_path))
        # The current mix: .mcp.json/.cursor/skills re-report "wrote", while
        # agents, hooks, and codex report explicit no-op statuses.
        assert _norm(second.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.claude/skills/nauro-loop/SKILL.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-planner.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-executor.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-reviewer.md\n"
            "  unchanged {TMP}/.claude/agents/nauro-tech-lead.md\n"
            "  {TMP}/repo: nauro hook already present in .claude/settings.json\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-ship-task.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-context.mdc\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-loop.mdc\n"
            "Codex: nauro already configured in {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-ship-task/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-context/SKILL.md\n"
            "  wrote {TMP}/.agents/skills/nauro-loop/SKILL.md\n"
            "  {TMP}/repo: nauro hooks already present in .codex/hooks.json\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n"
            + CONNECTOR_NOTICE_LINE
            + "\n"
            + HOOKS_NOTICE_LINE
            + "\n"
            + CODEX_HOOKS_NOTICE_LINE
            + "\n"
            + ALL_RESTART_LINE
            + "\n"
            + TRY_IT_LINE
        )
        assert second.stderr == ""


# ─── partial failure end-to-end ──────────────────────────────────────────────


class TestPartialFailure:
    def test_cursor_symlink_in_one_repo_of_two(self, tmp_path: Path, monkeypatch):
        """A symlinked .cursor in repo1 refuses only that repo's Cursor artifacts.

        Everything else (both .mcp.json files, repo2's Cursor pair, codex,
        skills, both AGENTS.md files) is still configured, and the command
        exits 0.
        """
        _pid, _store, paths = _register_project(tmp_path, monkeypatch, repos=("repo1", "repo2"))
        outside = tmp_path / "outside"
        outside.mkdir()
        (paths[0] / ".cursor").symlink_to(outside)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo1: wrote nauro to .mcp.json\n"
            "  {TMP}/repo2: wrote nauro to .mcp.json\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo1: refused to modify {TMP}/repo1/.cursor/mcp.json:"
            " {TMP}/repo1/.cursor is a symlink; Nauro does not write through symlinks in a"
            " repo checkout\n"
            "  {TMP}/repo1: refused to modify {TMP}/repo1/.cursor/rules/nauro-adopt.mdc:"
            " {TMP}/repo1/.cursor is a symlink; Nauro does not write through symlinks in a"
            " repo checkout\n"
            "  {TMP}/repo2: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo2/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo1: regenerated AGENTS.md\n"
            "  {TMP}/repo2: regenerated AGENTS.md\n"
            "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""
        # Refused path untouched: nothing written through the planted link.
        assert (paths[0] / ".cursor").is_symlink()
        assert list(outside.iterdir()) == []
        # Everything else configured.
        assert (paths[0] / ".mcp.json").is_file()
        assert (paths[0] / "AGENTS.md").is_file()
        assert (paths[1] / ".cursor" / "mcp.json").is_file()
        assert (paths[1] / ".cursor" / "rules" / "nauro-adopt.mdc").is_file()
        assert (tmp_path / ".codex" / "config.toml").is_file()
        assert (tmp_path / ".claude" / "skills" / "nauro-adopt" / "SKILL.md").is_file()

    def test_mcp_json_symlink_single_repo(self, tmp_path: Path, monkeypatch):
        """A symlinked .mcp.json refuses only that artifact; the rest proceeds."""
        _pid, _store, paths = _register_project(tmp_path, monkeypatch)
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        (paths[0] / ".mcp.json").symlink_to(outside)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == (
            "Configured Nauro for project 'proj' across all surfaces:\n"
            "\n"
            "  {TMP}/repo: refused to modify {TMP}/repo/.mcp.json: it is a symlink; Nauro"
            " does not write through symlinks in a repo checkout\n"
            "  wrote {TMP}/.claude/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: wrote nauro to .cursor/mcp.json\n"
            "  wrote {TMP}/repo/.cursor/rules/nauro-adopt.mdc\n"
            "Codex: wrote nauro to {TMP}/.codex/config.toml\n"
            "  wrote {TMP}/.agents/skills/nauro-adopt/SKILL.md\n"
            "  {TMP}/repo: regenerated AGENTS.md\n"
            "\n" + ALL_RESTART_LINE + "\n" + TRY_IT_LINE
        )
        assert result.stderr == ""
        assert (paths[0] / ".mcp.json").is_symlink()
        assert outside.read_text() == "{}"
        assert (paths[0] / ".cursor" / "mcp.json").is_file()
        assert (paths[0] / "AGENTS.md").is_file()


# ─── resolver warning shape ──────────────────────────────────────────────────


def _interpreter_sibling() -> str:
    """The nauro console script next to the running interpreter.

    Mirrors the resolver's sibling discovery so the expected {NAURO_CMD}
    value can be computed without importing the setup module. Both warning
    variants resolve to the sibling when one exists; skip when the test
    interpreter has none, since the resolver would then pick a different
    fallback shape.
    """
    bindir = Path(sys.executable).parent
    for name in ("nauro", "nauro.exe"):
        candidate = bindir / name
        if candidate.is_file():
            return str(candidate)
    pytest.skip("no nauro console script next to the test interpreter")


class TestResolverWarningShape:
    def test_fragile_path_warning_once_on_stderr(self, tmp_path: Path, monkeypatch):
        """Nothing durable but the sibling runs: one fragile warning per invocation."""
        from nauro.cli import utils as cli_utils

        sibling = _interpreter_sibling()
        monkeypatch.setattr(cli_utils, "_is_durable_install_path", lambda path: False)
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == _all_add_plain_expected()
        assert normalize_transcript(
            result.stderr, {sibling: "{NAURO_CMD}", str(tmp_path): "{TMP}"}
        ) == (
            "WARNING: recording nauro from a project virtualenv ({NAURO_CMD}).\n"
            "  This path breaks if the repo's virtualenv is rebuilt, moved, or corrupted,"
            " silently killing Nauro's MCP server and hooks. Install nauro durably (pipx"
            " install nauro, or uv tool install nauro) and re-run 'nauro setup all'.\n"
        )

    def test_unresolved_warning_once_on_stderr(self, tmp_path: Path, monkeypatch):
        """No candidate runs at all: one unresolved warning per invocation."""
        from nauro.cli import utils as cli_utils

        sibling = _interpreter_sibling()
        monkeypatch.setattr(cli_utils, "probe_nauro_command", lambda cmd, **kwargs: False)
        _register_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["setup", "all"])

        assert result.exit_code == 0
        assert _norm(result.stdout, tmp_path) == _all_add_plain_expected()
        assert normalize_transcript(
            result.stderr, {sibling: "{NAURO_CMD}", str(tmp_path): "{TMP}"}
        ) == (
            "WARNING: could not validate a working nauro; recorded '{NAURO_CMD}'.\n"
            "  Nauro's MCP server and hooks will not work until nauro is installed on a"
            " durable PATH (pipx install nauro, or uv tool install nauro), then re-run"
            " 'nauro setup all'.\n"
        )
