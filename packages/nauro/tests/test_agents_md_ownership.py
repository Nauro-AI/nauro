"""AGENTS.md ownership: only ``nauro sync`` overwrites hand-written files.

``regenerate_agents_md_for_project`` preserves an existing AGENTS.md that
Nauro did not generate unless the caller passes ``overwrite_unmanaged=True``,
which only ``nauro sync`` does. Every other write path (setup, adopt, init,
attach, note, propose_decision) leaves a hand-written file byte-identical
and warns. A symlinked AGENTS.md is never written through, even on the
overwrite path.
"""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.agents_md import regenerate_agents_md_for_project
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import register_v2_repo

runner = CliRunner()

SENTINEL = b"# Hand-written agent rules\n\nKeep these instructions.\n"


def test_regenerate_default_preserves_unmanaged_agents_md(tmp_path: Path):
    """Without an explicit overwrite grant, a marker-less file is untouched."""
    v2 = register_v2_repo(tmp_path, "preserveproj", chdir=False)
    (v2.repo / "AGENTS.md").write_bytes(SENTINEL)

    updated = regenerate_agents_md_for_project(v2.pid, v2.store_path)

    assert updated == []
    assert (v2.repo / "AGENTS.md").read_bytes() == SENTINEL


def test_regenerate_overwrite_unmanaged_replaces(tmp_path: Path):
    """The sync-only overwrite grant replaces a marker-less file."""
    v2 = register_v2_repo(tmp_path, "overwriteproj", chdir=False)
    (v2.repo / "AGENTS.md").write_bytes(SENTINEL)

    updated = regenerate_agents_md_for_project(v2.pid, v2.store_path, overwrite_unmanaged=True)

    assert updated == [v2.repo]
    content = (v2.repo / "AGENTS.md").read_text()
    assert "Keep these instructions." not in content
    assert "## Project: overwriteproj" in content


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_regenerate_refuses_symlink_even_with_overwrite(tmp_path: Path):
    """A symlinked AGENTS.md is skipped even under overwrite_unmanaged=True."""
    v2 = register_v2_repo(tmp_path, "linkproj", chdir=False)
    outside = tmp_path / "outside-agents.md"
    outside.write_text("untouchable")
    (v2.repo / "AGENTS.md").symlink_to(outside)

    updated = regenerate_agents_md_for_project(v2.pid, v2.store_path, overwrite_unmanaged=True)

    assert updated == []
    assert (v2.repo / "AGENTS.md").is_symlink()
    assert outside.read_text() == "untouchable"


def test_setup_all_preserves_hand_written_agents_md(tmp_path: Path, monkeypatch):
    """``setup all`` leaves a hand-written AGENTS.md byte-identical and warns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _, store_path = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store_path)
    monkeypatch.chdir(repo)
    (repo / "AGENTS.md").write_bytes(SENTINEL)

    result = runner.invoke(app, ["setup", "all"])
    assert result.exit_code == 0, result.output

    assert (repo / "AGENTS.md").read_bytes() == SENTINEL
    assert "existing AGENTS.md is not Nauro-generated" in result.output
    assert "regenerated AGENTS.md" not in result.output
