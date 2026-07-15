"""Registry data-integrity tests for `nauro init`.

* init refuses to mint a second registry entry for a repo an existing
  project already claims (the duplicate-entry footgun), even under --force.
* register_project_v2 validates the project name before any registry write,
  so garbage names never leak a half-written entry.

CWD and NAURO_HOME are both isolated to tmp_path by autouse conftest
fixtures; tests that need a specific cwd override on the same monkeypatch.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.registry import find_projects_by_name_v2, register_project_v2

runner = CliRunner()


def _git_init(repo) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


# ── duplicate-claim refusal ────────────────────────────────────────────────────


def test_init_force_refuses_already_claimed_repo(tmp_path, monkeypatch):
    """`init projF` → note → `init projF --force` must not mint a second entry.

    --force bypasses the cwd-config overwrite guard, but the repo is still
    claimed by the first projF; re-initializing would shadow that association
    with a duplicate registry entry. The refusal is independent of --force and
    exits 1. The earlier decision must survive in the original store.
    """
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["init", "projF"])
    assert first.exit_code == 0, first.output

    matches = find_projects_by_name_v2("projF")
    assert len(matches) == 1
    pid, _entry = matches[0]

    note_res = runner.invoke(app, ["note", "Chose X for Y reasons"])
    assert note_res.exit_code == 0, note_res.output
    decisions_dir = tmp_path / "projects" / pid / "decisions"
    seeded = sorted(decisions_dir.glob("*.md"))
    assert seeded, "the note should have written a decision file"

    forced = runner.invoke(app, ["init", "projF", "--force"])
    assert forced.exit_code == 1, forced.output

    # Still exactly one projF entry; the decision survived untouched.
    assert len(find_projects_by_name_v2("projF")) == 1
    assert sorted((tmp_path / "projects" / pid / "decisions").glob("*.md")) == seeded


def test_demo_reuse_refuses_repo_claimed_by_another_project(tmp_path, monkeypatch):
    real_repo = tmp_path / "real"
    demo_repo = tmp_path / "demo"
    real_repo.mkdir()
    demo_repo.mkdir()

    monkeypatch.chdir(real_repo)
    real = runner.invoke(app, ["init", "real-project"])
    assert real.exit_code == 0, real.output
    real_pid, _entry = find_projects_by_name_v2("real-project")[0]
    (real_repo / ".nauro" / "config.json").unlink()
    sentinel = b"# Hand-authored agent rules\n\nKeep this file.\n"
    (real_repo / "AGENTS.md").write_bytes(sentinel)

    monkeypatch.chdir(demo_repo)
    demo = runner.invoke(app, ["init", "--demo"])
    assert demo.exit_code == 0, demo.output
    demo_pid, _entry = find_projects_by_name_v2("demo-project")[0]

    monkeypatch.chdir(real_repo)
    reused = runner.invoke(app, ["init", "--demo"])

    assert reused.exit_code == 1, reused.output
    assert "already part of project 'real-project'" in reused.output
    assert registry.get_project_v2(real_pid)["repo_paths"] == [str(real_repo.resolve())]
    assert str(real_repo.resolve()) not in registry.get_project_v2(demo_pid)["repo_paths"]
    assert (real_repo / "AGENTS.md").read_bytes() == sentinel


def test_add_repo_refuses_repo_claimed_by_another_project(tmp_path, monkeypatch):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    monkeypatch.chdir(repo_a)
    assert runner.invoke(app, ["init", "project-a"]).exit_code == 0
    (repo_a / ".nauro" / "config.json").unlink()

    monkeypatch.chdir(repo_b)
    assert runner.invoke(app, ["init", "project-b"]).exit_code == 0
    result = runner.invoke(app, ["init", "project-b", "--add-repo", str(repo_a)])

    assert result.exit_code == 1, result.output
    assert "already part of project 'project-a'" in result.output
    project_b_id, project_b = find_projects_by_name_v2("project-b")[0]
    assert project_b["repo_paths"] == [str(repo_b.resolve())]
    assert registry.get_project_v2(project_b_id) == project_b


# ── project-name validation ────────────────────────────────────────────────────

# Names the locked validation rules reject: empty / whitespace-only,
# over-length, path separators, the '..' traversal substring, or a
# non-printable character. A single-leading-dot name (".hidden") is NOT in
# this set — the store path is ULID-keyed, so a leading dot is not a
# traversal risk and the locked rule does not enumerate it. ".dotdot.." below
# stands in for the dot-prefixed traversal case the rules do catch.
_INVALID_NAMES = [
    "",
    "   ",
    "x" * 300,
    "../escape",
    "foo/bar",
    ".dotdot..",
    "ctrl\x07char",
]


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_init_rejects_invalid_name_without_leaking_entry(bad_name, tmp_path, monkeypatch):
    """Invalid names exit 1 and leave no registry entry behind."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", bad_name])
    assert result.exit_code == 1, result.output
    assert find_projects_by_name_v2(bad_name) == []
    assert find_projects_by_name_v2(bad_name.strip()) == []


def test_init_accepts_valid_name(tmp_path, monkeypatch):
    """A valid name still registers and exits 0."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "valid-name_1"])
    assert result.exit_code == 0, result.output
    assert len(find_projects_by_name_v2("valid-name_1")) == 1


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_register_project_v2_raises_on_invalid_name(bad_name, tmp_path, monkeypatch):
    """register_project_v2 raises ValueError before touching the registry."""
    with pytest.raises(ValueError):
        register_project_v2(bad_name, [tmp_path])
    # No entry leaked even on the raising path.
    assert registry.load_registry_v2()["projects"] == {}


# ── same-name cross-repo fork: warn, don't silently fork ────────────────────────


def test_init_same_name_other_repo_warns_but_creates(tmp_path, monkeypatch):
    """`init shared` in a second repo still creates a project (v2 allows dup
    names) but must surface that it is a SEPARATE store and point at --add-repo,
    instead of silently forking the cross-repo value prop."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    monkeypatch.chdir(repo_a)
    first = runner.invoke(app, ["init", "shared"])
    assert first.exit_code == 0, first.output
    (pid_a, _entry) = find_projects_by_name_v2("shared")[0]

    monkeypatch.chdir(repo_b)
    second = runner.invoke(app, ["init", "shared"])
    assert second.exit_code == 0, second.output
    assert "SEPARATE store" in second.output
    assert "--add-repo" in second.output
    assert pid_a in second.output  # names the pre-existing project

    matches = find_projects_by_name_v2("shared")
    assert len(matches) == 2
    assert len({pid for pid, _ in matches}) == 2  # two distinct ids


def test_init_unique_name_has_no_collision_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "solo"])
    assert result.exit_code == 0, result.output
    assert "SEPARATE store" not in result.output


def test_init_add_repo_links_second_repo_to_one_project(tmp_path, monkeypatch):
    """The documented association path: --add-repo joins a second repo to the
    same project rather than forking a new one."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    monkeypatch.chdir(repo_a)
    assert runner.invoke(app, ["init", "linked"]).exit_code == 0

    monkeypatch.chdir(repo_b)
    res = runner.invoke(app, ["init", "linked", "--add-repo", "."])
    assert res.exit_code == 0, res.output

    # Still exactly one project named 'linked' — the second repo joined it.
    matches = find_projects_by_name_v2("linked")
    assert len(matches) == 1


# ── omitted-name default ────────────────────────────────────────────────────────


def test_init_without_name_derives_from_directory(tmp_path, monkeypatch):
    """A bare `nauro init` names the project after the directory, not the
    surprising literal 'demo-project'."""
    repo = tmp_path / "my-cool-repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert len(find_projects_by_name_v2("my-cool-repo")) == 1
    assert find_projects_by_name_v2("demo-project") == []


def test_init_demo_without_name_still_uses_demo_project(tmp_path, monkeypatch):
    """`nauro init --demo` (no name) keeps the fixed sample name."""
    repo = tmp_path / "anything"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["init", "--demo"])
    assert result.exit_code == 0, result.output
    assert len(find_projects_by_name_v2("demo-project")) == 1


def test_init_warns_for_unignored_repo_config_in_git_repo(tmp_path, monkeypatch):
    _git_init(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "gitproj"])
    assert result.exit_code == 0, result.output
    assert ".nauro/config.json is untracked and not git-ignored" in result.output
    assert "repo-local Nauro project config" in result.output
    assert "AGENTS.md is untracked and not git-ignored" in result.output
    assert "It contains generated Nauro context" in result.output


def test_init_suppresses_repo_config_warning_when_ignored(tmp_path, monkeypatch):
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(".nauro/config.json\nAGENTS.md\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "gitproj"])
    assert result.exit_code == 0, result.output
    assert ".nauro/config.json is untracked and not git-ignored" not in result.output
    assert "AGENTS.md is untracked and not git-ignored" not in result.output


# ── symlinked .nauro/config.json refusal ──────────────────────────────────────


_symlink_supported = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra Windows privileges"
)


@_symlink_supported
def test_init_refuses_symlinked_repo_config_before_registering(tmp_path, monkeypatch):
    """A pre-planted symlink at the config path aborts init before any registry write."""
    project_dir = tmp_path / "proj"
    (project_dir / ".nauro").mkdir(parents=True)
    (project_dir / ".nauro" / "config.json").symlink_to(tmp_path / "attacker.json")
    monkeypatch.chdir(project_dir)

    result = runner.invoke(app, ["init", "someproj"])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    assert find_projects_by_name_v2("someproj") == []
    assert not (tmp_path / "attacker.json").exists()


@_symlink_supported
def test_init_add_repo_refuses_symlinked_repo_config(tmp_path, monkeypatch):
    """--add-repo refuses a symlinked config path before extending the entry."""
    first = tmp_path / "first"
    first.mkdir()
    monkeypatch.chdir(first)
    assert runner.invoke(app, ["init", "projS"]).exit_code == 0

    second = tmp_path / "second"
    (second / ".nauro").mkdir(parents=True)
    (second / ".nauro" / "config.json").symlink_to(tmp_path / "attacker.json")

    result = runner.invoke(app, ["init", "projS", "--add-repo", str(second)])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    _pid, entry = find_projects_by_name_v2("projS")[0]
    assert str(second.resolve()) not in entry["repo_paths"]
    assert not (tmp_path / "attacker.json").exists()


@_symlink_supported
def test_init_demo_refuses_symlinked_repo_config(tmp_path, monkeypatch):
    """--demo refuses a symlinked config path before registering the demo project."""
    project_dir = tmp_path / "demo-dir"
    (project_dir / ".nauro").mkdir(parents=True)
    (project_dir / ".nauro" / "config.json").symlink_to(tmp_path / "attacker.json")
    monkeypatch.chdir(project_dir)

    result = runner.invoke(app, ["init", "--demo"])

    assert result.exit_code == 1
    assert "refused to modify" in result.output
    assert find_projects_by_name_v2("demo-project") == []
    assert not (tmp_path / "attacker.json").exists()
