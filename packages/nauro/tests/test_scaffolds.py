"""Tests for project store scaffold templates."""

from pathlib import Path

from nauro.templates.scaffolds import STATE_CURRENT_MD, scaffold_project_store


def test_state_scaffold_does_not_advertise_cli_update_state():
    """The empty-state placeholder must not tell users to 'call update_state'.

    `update_state` is an MCP write tool, not a CLI command. The earlier
    scaffold copy ("...— call update_state to capture progress.") flowed
    into AGENTS.md and led a real user to try `nauro update_state` from
    the shell, which prints "No such command". This invariant prevents the
    misleading instruction from coming back.
    """
    assert "update_state" not in STATE_CURRENT_MD
    assert "No state recorded yet" in STATE_CURRENT_MD


def test_scaffold_project_store_writes_clean_state_placeholder(tmp_path: Path):
    """`scaffold_project_store` writes the cleaned-up state placeholder."""
    scaffold_project_store("testproj", tmp_path)

    state = (tmp_path / "state_current.md").read_text()
    assert "update_state" not in state
    assert "No state recorded yet" in state
