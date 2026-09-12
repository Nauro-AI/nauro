"""Every setup codec reports a write it could not land as a typed outcome, and setup exits 1."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.integrations import codex_config, orchestrator
from nauro.cli.integrations.agents import materialize_agents_cursor_for_repo
from nauro.cli.integrations.codex_config import _configure_codex
from nauro.cli.integrations.codex_hooks import materialize_hooks_codex
from nauro.cli.integrations.json_mcp import _configure_mcp
from nauro.cli.integrations.skills import (
    materialize_skills_codex,
    materialize_skills_cursor_for_repo,
)
from nauro.cli.main import app
from nauro.setup.outcomes import (
    AgentKind,
    AgentsMdKind,
    CodexConfigKind,
    CodexHookKind,
    JsonMcpKind,
    RawLine,
    SkillKind,
    WriteFailure,
    is_failure,
)
from nauro.setup.render import render
from nauro.store import _atomic
from nauro.store.registry import find_projects_by_name_v2, register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_json_mcp_reports_a_config_it_cannot_write(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".mcp.json").mkdir()

    outcome = _configure_mcp(repo, remove=False)

    assert outcome.kind is JsonMcpKind.WRITE_FAILED
    assert outcome.write_failure is not None
    assert outcome.write_failure.path == repo / ".mcp.json"
    assert outcome.write_failure.errno is not None
    assert is_failure(outcome)


def test_codex_config_reports_a_config_it_cannot_write(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.mkdir()

    outcome = _configure_codex(remove=False, config_path=config)

    assert outcome.kind is CodexConfigKind.WRITE_FAILED
    assert outcome.write_failure is not None
    assert outcome.write_failure.path == config


def test_codex_hooks_report_a_file_they_cannot_write(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".codex" / "hooks.json").mkdir(parents=True)

    outcome = materialize_hooks_codex(repo, remove=False)

    assert outcome.kind is CodexHookKind.WRITE_FAILED
    assert outcome.write_failure is not None
    assert outcome.write_failure.path == repo / ".codex" / "hooks.json"


def test_skill_and_agent_files_that_cannot_be_written_are_reported_per_file(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".cursor" / "rules" / "nauro-adopt.mdc").mkdir(parents=True)
    (repo / ".cursor" / "agents" / "nauro-planner.md").mkdir(parents=True)

    skills = materialize_skills_cursor_for_repo(repo, remove=False)
    agents = materialize_agents_cursor_for_repo(repo, remove=False)

    failed_skills = [o for o in skills if o.kind is SkillKind.WRITE_FAILED]
    assert [o.target for o in failed_skills] == [repo / ".cursor" / "rules" / "nauro-adopt.mdc"]
    assert failed_skills[0].write_failure is not None
    failed_agents = [o for o in agents if o.kind is AgentKind.WRITE_FAILED]
    assert [o.target for o in failed_agents] == [repo / ".cursor" / "agents" / "nauro-planner.md"]
    assert any(o.kind is AgentKind.INSTALLED for o in agents)


def test_is_failure_ignores_raw_lines_and_successes(tmp_path: Path):
    repo = _repo(tmp_path)
    assert not is_failure(RawLine("header"))
    assert not is_failure(_configure_mcp(repo, remove=False))


def test_setup_exits_nonzero_when_a_codec_could_not_write(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    _pid, store = register_project_v2("proj", [repo])
    scaffold_project_store("proj", store)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        codex_config, "codex_config_path", lambda: tmp_path / "codex" / "config.toml"
    )
    (repo / ".mcp.json").mkdir()

    result = runner.invoke(app, ["setup", "claude-code"])

    assert result.exit_code == 1, result.output
    assert f"{repo}: could not write {repo / '.mcp.json'}" in result.output

    (repo / ".mcp.json").rmdir()
    assert runner.invoke(app, ["setup", "claude-code"]).exit_code == 0


def test_a_failure_inside_the_atomic_write_names_the_target(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".mcp.json").write_text("{}", encoding="utf-8")

    def _denied(src, dst):
        raise PermissionError(errno.EACCES, "Permission denied", str(src))

    monkeypatch.setattr(_atomic.os, "replace", _denied)

    outcome = _configure_mcp(repo, remove=False)

    assert outcome.kind is JsonMcpKind.WRITE_FAILED
    assert outcome.write_failure == WriteFailure(
        repo / ".mcp.json", errno.EACCES, "Permission denied"
    )


def test_skill_and_agent_files_that_are_not_utf8_are_preserved(tmp_path: Path):
    repo = _repo(tmp_path)
    skill = repo / ".cursor" / "rules" / "nauro-adopt.mdc"
    agent = repo / ".cursor" / "agents" / "nauro-planner.md"
    for path in (skill, agent):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe not text")

    kinds = {
        (o.target, remove): o.kind
        for remove in (False, True)
        for o in (
            *materialize_skills_cursor_for_repo(repo, remove=remove),
            *materialize_agents_cursor_for_repo(repo, remove=remove),
        )
        if o.target in (skill, agent)
    }

    assert kinds == {
        (skill, False): SkillKind.PRESERVED_UNDECODABLE,
        (skill, True): SkillKind.PRESERVED_UNDECODABLE,
        (agent, False): AgentKind.PRESERVED_UNDECODABLE,
        (agent, True): AgentKind.PRESERVED_UNDECODABLE,
    }
    assert skill.read_bytes() == agent.read_bytes() == b"\xff\xfe not text"


def test_legacy_codex_skill_stays_when_its_replacement_was_not_written():
    legacy = Path.home() / ".codex" / "skills" / "nauro-adopt" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    (Path.home() / ".agents" / "skills" / "nauro-adopt" / "SKILL.md").mkdir(parents=True)

    outcomes = materialize_skills_codex(remove=False)

    assert [o.kind for o in outcomes] == [SkillKind.WRITE_FAILED]
    assert legacy.read_text(encoding="utf-8") == "legacy"


def test_agents_md_regeneration_failure_is_one_typed_outcome(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    pid, store = register_project_v2("proj", [repo])
    scaffold_project_store("proj", store)

    def _no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(orchestrator, "warn_then_regen", _no_space)

    outcomes = orchestrator.claude_code_surfaces(
        [repo], remove=False, with_hooks=False, store_name=pid, store_path=store, warn=print
    )

    failures = [o for o in outcomes if is_failure(o)]
    assert [o.kind for o in failures] == [AgentsMdKind.WRITE_FAILED]
    assert render(failures[0]) == ["AGENTS.md regeneration: write failed - No space left on device"]


def test_adopt_exits_nonzero_when_a_codec_could_not_write(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    (repo / ".mcp.json").mkdir()

    result = runner.invoke(app, ["adopt", "--name", "alpha"])

    assert result.exit_code == 1, result.output
    assert f"could not write {repo / '.mcp.json'}" in result.output
    assert "re-run 'nauro setup all'" in result.output
    assert "Next: restart your agent" not in result.output


def test_unadopt_keeps_the_adoption_when_a_removal_could_not_be_written(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    assert runner.invoke(app, ["adopt", "--name", "alpha"]).exit_code == 0
    (repo / ".mcp.json").unlink()
    (repo / ".mcp.json").mkdir()

    result = runner.invoke(app, ["adopt", "--remove", "--yes"])

    assert result.exit_code == 1, result.output
    assert "re-run 'nauro adopt --remove'" in result.output
    assert (repo / ".nauro" / "config.json").is_file()
    assert len(find_projects_by_name_v2("alpha")) == 1


def test_a_claude_md_bridge_that_cannot_be_written_fails_setup(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    _pid, store = register_project_v2("proj", [repo])
    scaffold_project_store("proj", store)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        codex_config, "codex_config_path", lambda: tmp_path / "codex" / "config.toml"
    )
    (repo / "CLAUDE.md").mkdir()

    result = runner.invoke(app, ["setup", "all"])

    assert result.exit_code == 1, result.output
    assert f"{repo}: CLAUDE.md bridge error - CLAUDE.md is not a regular file" in result.output
