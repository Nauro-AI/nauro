"""Tests for the Nauro CLI commands."""

from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import get_project, register_project
from nauro.store.snapshot import capture_snapshot
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def test_app_shows_help():
    """Running nauro with no args should show help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "nauro" in result.output.lower()


def test_init_command(tmp_path: Path, monkeypatch):
    """nauro init should create an id-keyed project store."""
    from nauro.store.registry import find_projects_by_name_v2

    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "testproj"])
    assert result.exit_code == 0
    assert "Initialized project" in result.output
    assert "Next:" in result.output

    matches = find_projects_by_name_v2("testproj")
    assert len(matches) == 1
    pid, _entry = matches[0]
    assert (tmp_path / "projects" / pid / "project.md").exists()
    assert (tmp_path / "projects" / pid / "decisions" / "001-initial-setup.md").exists()


def test_note_command(tmp_path: Path, monkeypatch):
    """nauro note should accept a message."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Use Postgres"])
    assert result.exit_code == 0
    assert "Decision recorded" in result.output
    # 002 because 001-initial-setup is scaffolded — now shows full path
    assert "002-use-postgres.md" in result.output


def test_sync_command(tmp_path: Path, monkeypatch):
    """nauro sync should capture a snapshot."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "Synced myproj" in result.output


def test_log_command(tmp_path: Path, monkeypatch):
    """nauro log should list snapshots."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    capture_snapshot(store, trigger="test sync")

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "v001" in result.output


# --- --project flag tests ---


def test_note_with_project_flag_overrides_cwd(tmp_path: Path, monkeypatch):
    """--project flag should resolve the named project regardless of cwd."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    # Register two projects
    store_a = register_project("alpha", [tmp_path / "repo_a"])
    store_b = register_project("beta", [tmp_path / "repo_b"])
    scaffold_project_store("alpha", store_a)
    scaffold_project_store("beta", store_b)

    # cwd is repo_a, but --project targets beta
    (tmp_path / "repo_a").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "repo_a")

    result = runner.invoke(app, ["note", "--project", "beta", "Targeted decision"])
    assert result.exit_code == 0
    assert "beta" in result.output
    # Decision should be in beta's store (002+), not alpha's (only 001-initial-setup)
    beta_decisions = [
        f for f in (store_b / "decisions").glob("*.md") if not f.name.startswith("001-")
    ]
    alpha_decisions = [
        f for f in (store_a / "decisions").glob("*.md") if not f.name.startswith("001-")
    ]
    assert beta_decisions
    assert not alpha_decisions


def test_project_flag_unknown_name_gives_error(tmp_path: Path, monkeypatch):
    """--project with an unknown name should error and list available projects."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("realproj", [tmp_path])
    scaffold_project_store("realproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "--project", "nope", "Test"])
    assert result.exit_code == 1
    assert "Unknown project 'nope'" in result.output
    assert "realproj" in result.output


def test_no_project_flag_no_cwd_match_gives_error(tmp_path: Path, monkeypatch):
    """Missing --project and no cwd match should error with available projects."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("faraway", [tmp_path / "elsewhere"])
    scaffold_project_store("faraway", store)

    # cwd doesn't match any registered repo
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    result = runner.invoke(app, ["note", "Orphan decision"])
    assert result.exit_code == 1
    assert "No project found" in result.output
    assert "faraway" in result.output
    assert "--project" in result.output


def test_no_cwd_match_suggests_project_by_dirname(tmp_path: Path, monkeypatch):
    """When cwd dirname matches a project name, suggest adding the repo path."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    old_repo = tmp_path / "old_location" / "myapp"
    old_repo.mkdir(parents=True)
    store = register_project("myapp", [old_repo])
    scaffold_project_store("myapp", store)

    # Clone/move to a new location with same dirname
    new_repo = tmp_path / "new_location" / "myapp"
    new_repo.mkdir(parents=True)
    monkeypatch.chdir(new_repo)

    result = runner.invoke(app, ["note", "test"])
    assert result.exit_code == 1
    assert "project 'myapp' exists but this path is not registered" in result.output
    assert "nauro init myapp --add-repo" in result.output


def test_sync_with_project_flag(tmp_path: Path, monkeypatch):
    """nauro sync --project should target the named project."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path / "repo"])
    scaffold_project_store("myproj", store)

    # cwd is unrelated
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    result = runner.invoke(app, ["sync", "--project", "myproj"])
    assert result.exit_code == 0
    assert "Synced myproj" in result.output


def test_log_with_project_flag(tmp_path: Path, monkeypatch):
    """nauro log --project should target the named project."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("myproj", [tmp_path / "repo"])
    scaffold_project_store("myproj", store)
    capture_snapshot(store, trigger="test")

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    result = runner.invoke(app, ["log", "--project", "myproj"])
    assert result.exit_code == 0
    assert "v001" in result.output


def test_get_project_returns_entry(tmp_path: Path, monkeypatch):
    """get_project should return the entry dict for a known project."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    register_project("proj", [tmp_path / "repo"])
    entry = get_project("proj")
    assert entry is not None
    assert "repo_paths" in entry


def test_get_project_returns_none_for_unknown(tmp_path: Path, monkeypatch):
    """get_project should return None for unknown projects."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    assert get_project("nonexistent") is None
