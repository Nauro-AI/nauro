"""Tests for project store scaffold templates."""

from pathlib import Path

from nauro_core.parsing import is_scaffold_project_md

from nauro.templates.scaffolds import (
    PROJECT_MD,
    STATE_CURRENT_MD,
    render_scaffold,
    scaffold_project_store,
)


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


def test_rendered_project_scaffold_matches_kernel_guard():
    """The rendered project.md scaffold is recognized by the kernel's guard.

    Cross-package drift pin: the template here and the scaffold-form check in
    nauro-core (which lets build_l0 skip unedited scaffolds) must compose from
    the same body constant. If either side drifts, freshly scaffolded stores
    start leaking placeholder prompts into every L0 payload.
    """
    assert is_scaffold_project_md(render_scaffold(PROJECT_MD, project_name="x"))
