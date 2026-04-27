"""Tests for nauro.store.registry and nauro init."""

import json

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.repo_config import load_repo_config
from nauro.templates.scaffolds import scaffold_project_store


def _patch_home(monkeypatch, tmp_path):
    """Point NAURO_HOME at a temp directory."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


# --- Registry CRUD ---


def test_load_registry_empty(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    data = registry.load_registry()
    assert data == {"projects": {}, "schema_version": 1}


def test_save_and_load_registry(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    data = {"projects": {"myproj": {"repo_paths": ["/tmp/repo"]}}}
    registry.save_registry(data)
    loaded = registry.load_registry()
    assert loaded["projects"] == data["projects"]
    assert loaded["schema_version"] == 1


def test_register_project(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = registry.register_project("proj1", [repo])
    assert store_path.exists()
    assert store_path.name == "proj1"
    data = registry.load_registry()
    assert "proj1" in data["projects"]
    assert str(repo.resolve()) in data["projects"]["proj1"]["repo_paths"]


def test_register_duplicate_raises(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("dup", [repo])
    try:
        registry.register_project("dup", [repo])
        assert False, "Expected ValueError"
    except ValueError:
        pass


def test_add_repo(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    registry.register_project("proj", [repo1])
    registry.add_repo("proj", repo2)
    data = registry.load_registry()
    paths = data["projects"]["proj"]["repo_paths"]
    assert str(repo2.resolve()) in paths


def test_add_repo_idempotent(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    registry.add_repo("proj", repo)
    data = registry.load_registry()
    assert len(data["projects"]["proj"]["repo_paths"]) == 1


def test_add_repo_missing_project(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    try:
        registry.add_repo("nope", tmp_path)
        assert False, "Expected KeyError"
    except KeyError:
        pass


# --- resolve_project ---


def test_resolve_project_exact(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    assert registry.resolve_project(repo) == "proj"


def test_resolve_project_nested(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    registry.register_project("proj", [repo])
    assert registry.resolve_project(nested) == "proj"


def test_resolve_project_no_match(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    other = tmp_path / "other"
    other.mkdir()
    assert registry.resolve_project(other) is None


# --- find_stale_paths ---


def test_find_stale_paths_none(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    assert registry.find_stale_paths() == []


def test_find_stale_paths_detects_missing(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    repo.rmdir()
    stale = registry.find_stale_paths()
    assert len(stale) == 1
    assert stale[0][0] == "proj"


# --- suggest_project_for_path ---


def test_suggest_project_matching_dirname(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "myapp"
    repo.mkdir()
    registry.register_project("myapp", [repo])
    # Different path, same directory name
    other = tmp_path / "clones" / "myapp"
    other.mkdir(parents=True)
    assert registry.suggest_project_for_path(other) == "myapp"


def test_suggest_project_no_match(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "myapp"
    repo.mkdir()
    registry.register_project("myapp", [repo])
    other = tmp_path / "unrelated"
    other.mkdir()
    assert registry.suggest_project_for_path(other) is None


# --- scaffold ---


def test_scaffold_creates_all_files(tmp_path):
    store = tmp_path / "store"
    scaffold_project_store("testproj", store)
    assert (store / "project.md").exists()
    assert (store / "state.md").exists()
    assert (store / "stack.md").exists()
    assert (store / "open-questions.md").exists()
    assert (store / "decisions").is_dir()
    assert (store / "snapshots").is_dir()
    content = (store / "project.md").read_text()
    assert "# testproj" in content


# --- CLI init ---

runner = CliRunner()


def _v2_entry_for_name(name: str) -> tuple[str, dict]:
    """Return the single v2 (project_id, entry) matching ``name``.

    Tests assert one entry exists; extracted helper makes the assertions read clean.
    """
    matches = registry.find_projects_by_name_v2(name)
    assert len(matches) == 1, f"expected one v2 entry for {name!r}, got {len(matches)}"
    return matches[0]


def test_init_cli(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "myproject"])
    assert result.exit_code == 0
    assert "Initialized project 'myproject'" in result.output
    assert "Store:" in result.output
    pid, _entry = _v2_entry_for_name("myproject")
    store = tmp_path / "nauro_home" / "projects" / pid
    assert (store / "project.md").exists()


def test_init_cli_writes_repo_config_local(tmp_path, monkeypatch):
    """`nauro init <name>` writes a local-mode .nauro/config.json into cwd."""
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "configproj"])
    assert result.exit_code == 0
    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "local"
    assert cfg["name"] == "configproj"
    pid, _entry = _v2_entry_for_name("configproj")
    assert cfg["id"] == pid


def test_init_cli_with_add_repo(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    result = runner.invoke(app, ["init", "proj2", "--add-repo", str(repo)])
    assert result.exit_code == 0
    assert str(repo.resolve()) in result.output
    cfg = load_repo_config(repo)
    assert cfg["name"] == "proj2"


def test_init_cli_duplicate(tmp_path, monkeypatch):
    """A second `nauro init <same-name>` (no --add-repo) creates a SECOND project.

    v2 allows duplicate names — id is unique. To extend an existing project,
    callers must pass --add-repo, which is the explicit signal.
    """
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "dup"])
    result = runner.invoke(app, ["init", "dup"])
    # Either succeeds (creates a second, distinct id-keyed entry) or fails with
    # the clear duplicate-id message; both are acceptable, but the registry
    # ends up with two entries named 'dup' in the success case.
    assert result.exit_code == 0
    assert len(registry.find_projects_by_name_v2("dup")) == 2


def test_init_cli_add_repo_to_existing(tmp_path, monkeypatch):
    """nauro init <existing> --add-repo should add repo instead of failing."""
    _patch_home(monkeypatch, tmp_path)
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    result = runner.invoke(app, ["init", "proj", "--add-repo", str(repo1)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["init", "proj", "--add-repo", str(repo2)])
    assert result.exit_code == 0
    assert "Updated project" in result.output
    assert "Added repo" in result.output
    pid, entry = _v2_entry_for_name("proj")
    paths = entry["repo_paths"]
    assert str(repo2.resolve()) in paths
    # v2 registry shape
    raw = json.loads((tmp_path / "nauro_home" / "registry.json").read_text())
    assert raw["schema_version"] == 2
    assert pid in raw["projects"]


def test_remove_repo(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    removed = registry.remove_repo("proj", str(repo.resolve()))
    assert removed is True
    data = registry.load_registry()
    assert str(repo.resolve()) not in data["projects"]["proj"]["repo_paths"]


def test_remove_repo_not_found(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("proj", [repo])
    assert registry.remove_repo("proj", "/nonexistent") is False
