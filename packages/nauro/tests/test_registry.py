"""Tests for nauro.store.registry and nauro init."""

import json

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.repo_config import load_repo_config
from nauro.templates.scaffolds import scaffold_project_store

# --- suggest_project_for_path ---


def test_suggest_project_matching_dirname(tmp_path, monkeypatch):
    repo = tmp_path / "myapp"
    repo.mkdir()
    pid, _store = registry.register_project_v2("myapp", [repo])
    # Different path, same directory name
    other = tmp_path / "clones" / "myapp"
    other.mkdir(parents=True)
    suggested_pid, entry = registry.suggest_project_for_path(other)
    assert suggested_pid == pid
    assert entry["name"] == "myapp"


def test_suggest_project_no_match(tmp_path, monkeypatch):
    repo = tmp_path / "myapp"
    repo.mkdir()
    registry.register_project_v2("myapp", [repo])
    other = tmp_path / "unrelated"
    other.mkdir()
    assert registry.suggest_project_for_path(other) is None


def test_suggest_project_ambiguous_name_yields_none(tmp_path, monkeypatch):
    """Duplicate v2 names produce no suggestion: the hinted command refuses them."""
    repo_a = tmp_path / "a" / "myapp"
    repo_a.mkdir(parents=True)
    registry.register_project_v2("myapp", [repo_a])
    repo_b = tmp_path / "b" / "myapp"
    repo_b.mkdir(parents=True)
    registry.register_project_v2("myapp", [repo_b])
    other = tmp_path / "clones" / "myapp"
    other.mkdir(parents=True)
    assert registry.suggest_project_for_path(other) is None


# --- scaffold ---


def test_scaffold_creates_all_files(tmp_path):
    store = tmp_path / "store"
    scaffold_project_store("testproj", store)
    assert (store / "project.md").exists()
    assert (store / "state_current.md").exists()
    assert not (store / "state.md").exists()
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
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "myproject"])
    assert result.exit_code == 0
    assert "Initialized project 'myproject'" in result.output
    assert "Store:" in result.output
    pid, _entry = _v2_entry_for_name("myproject")
    store = tmp_path / "projects" / pid
    assert (store / "project.md").exists()


def test_init_cli_writes_repo_config_local(tmp_path, monkeypatch):
    """`nauro init <name>` writes a local-mode .nauro/config.json into cwd."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "configproj"])
    assert result.exit_code == 0
    cfg = load_repo_config(tmp_path)
    assert cfg["mode"] == "local"
    assert cfg["name"] == "configproj"
    pid, _entry = _v2_entry_for_name("configproj")
    assert cfg["id"] == pid


def test_init_cli_with_add_repo(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    result = runner.invoke(app, ["init", "proj2", "--add-repo", str(repo)])
    assert result.exit_code == 0
    assert str(repo.resolve()) in result.output
    cfg = load_repo_config(repo)
    assert cfg["name"] == "proj2"


def test_init_cli_duplicate(tmp_path, monkeypatch):
    """v2 allows duplicate names — id is unique. Two `nauro init dup`
    invocations from separate cwds both succeed and create distinct
    entries. From the SAME cwd, the overwrite guard refuses without
    --force; the registry-allows-dups invariant is verified by exercising
    each init from its own directory.
    """
    cwd1 = tmp_path / "cwd1"
    cwd2 = tmp_path / "cwd2"
    cwd1.mkdir()
    cwd2.mkdir()
    monkeypatch.chdir(cwd1)
    runner.invoke(app, ["init", "dup"])
    monkeypatch.chdir(cwd2)
    result = runner.invoke(app, ["init", "dup"])
    assert result.exit_code == 0
    assert len(registry.find_projects_by_name_v2("dup")) == 2


def test_init_cli_refuses_overwrite_without_force(tmp_path, monkeypatch):
    """A second `nauro init <other-name>` from the same cwd refuses to
    overwrite the existing .nauro/config.json without --force, naming the
    existing project so the caller can decide what to do."""
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "first"])
    assert first.exit_code == 0
    result = runner.invoke(app, ["init", "second"])
    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert "'first'" in result.output
    assert "'second'" in result.output
    assert "--force" in result.output
    # Existing config must be untouched.
    cfg = load_repo_config(tmp_path)
    assert cfg["name"] == "first"


def test_init_cli_force_does_not_duplicate_claimed_repo(tmp_path, monkeypatch):
    """--force overwrites only the cwd config — it does not mint a second
    registry entry for a repo an existing project already claims.

    --force bypasses the config-overwrite guard, but the repo remains claimed
    by 'first'; minting a 'second' entry for the same repo would shadow that
    association. init refuses (exit 1) and 'second' never enters the registry.
    """
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "first"])
    assert first.exit_code == 0, first.output
    result = runner.invoke(app, ["init", "second", "--force"])
    assert result.exit_code == 1, result.output
    assert len(registry.find_projects_by_name_v2("first")) == 1
    assert registry.find_projects_by_name_v2("second") == []


def test_init_cli_add_repo_idempotent_on_same_id(tmp_path, monkeypatch):
    """Re-running `nauro init <existing-name> --add-repo <repo>` against
    a repo already pointing at that project succeeds (idempotent), because
    the existing config id matches the project's id."""
    repo = tmp_path / "repo"
    repo.mkdir()
    first = runner.invoke(app, ["init", "proj", "--add-repo", str(repo)])
    assert first.exit_code == 0
    again = runner.invoke(app, ["init", "proj", "--add-repo", str(repo)])
    assert again.exit_code == 0, again.output


def test_init_cli_add_repo_refuses_overwrite_on_different_id(tmp_path, monkeypatch):
    """--add-repo against a repo already linked to a *different* project
    refuses without --force, naming both projects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Repo already linked to 'alpha'.
    first = runner.invoke(app, ["init", "alpha", "--add-repo", str(repo)])
    assert first.exit_code == 0
    # Attempt to link the same repo to a fresh 'beta' project.
    runner.invoke(app, ["init", "beta"], catch_exceptions=False)  # creates beta entry
    again = runner.invoke(app, ["init", "beta", "--add-repo", str(repo)], catch_exceptions=False)
    assert again.exit_code == 1
    assert "Refusing to overwrite" in again.output
    assert "'alpha'" in again.output
    # Existing config untouched.
    cfg = load_repo_config(repo)
    assert cfg["name"] == "alpha"


def test_init_cli_add_repo_to_existing(tmp_path, monkeypatch):
    """nauro init <existing> --add-repo should add repo instead of failing."""
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
    raw = json.loads((tmp_path / "registry.json").read_text())
    assert raw["schema_version"] == 2
    assert pid in raw["projects"]


def test_init_add_repo_to_existing_writes_per_repo_config(tmp_path, monkeypatch):
    """Regression: `--add-repo` against same-name project must write `.nauro/config.json`.

    The per-repo config is the source of truth for "is this repo adopted?".
    Without it, downstream guards (``nauro adopt`` already-adopted check,
    the /nauro-adopt skill's Step 2) fail to detect the linkage.
    """
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    runner.invoke(app, ["init", "proj", "--add-repo", str(repo1)])
    runner.invoke(app, ["init", "proj", "--add-repo", str(repo2)])

    # Both repos have a per-repo config pointing at the same project_id.
    cfg1 = load_repo_config(repo1)
    cfg2 = load_repo_config(repo2)
    assert cfg1["name"] == "proj"
    assert cfg2["name"] == "proj"
    assert cfg1["mode"] == "local"
    assert cfg2["mode"] == "local"
    assert cfg1["id"] == cfg2["id"]
    pid, _ = _v2_entry_for_name("proj")
    assert cfg2["id"] == pid


# --- Corrupt-shape tolerance ---


def test_load_registry_v2_non_dict_returns_empty(tmp_path, monkeypatch):
    """A registry.json that parses to a non-dict (valid JSON, wrong shape)
    falls back to the empty-registry default rather than crashing downstream."""
    (tmp_path / "registry.json").write_text("[]")
    data = registry.load_registry_v2()
    assert data == {"projects": {}, "schema_version": 2}


# --- get_store_path_v2 containment (defense-in-depth) ---


def test_get_store_path_v2_accepts_valid_ulid(tmp_path, monkeypatch):
    """A canonical ULID maps to the id-keyed directory under the projects dir."""
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    assert registry.get_store_path_v2(pid) == registry._projects_dir() / pid


@pytest.mark.parametrize("evil", ["../../../../etc", "../escape", "/etc"])
def test_get_store_path_v2_rejects_escape(tmp_path, monkeypatch, evil):
    """A project_id that resolves outside the projects dir is refused.

    ULID validation at the config boundary is the primary guard; this is the
    second layer that protects callers taking an id straight from argv (e.g.
    ``nauro attach <project_id>``) from escaping ~/.nauro/projects/.
    """
    with pytest.raises(ValueError, match="outside the project store"):
        registry.get_store_path_v2(evil)


def test_get_store_path_v2_allows_contained_non_canonical_id(tmp_path, monkeypatch):
    """Containment-only: a contained id that is not a canonical ULID (legacy /
    test-fixture ids) is still returned. This guard rejects escapes, not the
    full ULID alphabet — keeping it from breaking non-escaping callers."""
    pid = "01TESTPROJECT00000000000"
    assert registry.get_store_path_v2(pid) == registry._projects_dir() / pid


# --- nauro home directory permissions ---


def test_ensure_nauro_home_creates_owner_only(tmp_path, monkeypatch):
    """A fresh Nauro home is created at 0o700 so the token and store are not
    readable by other accounts on a shared host."""
    home = tmp_path / "fresh_home"
    monkeypatch.setenv("NAURO_HOME", str(home))
    created = registry._ensure_nauro_home()
    assert created == home
    assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_ensure_nauro_home_tightens_existing_wide_dir(tmp_path, monkeypatch):
    """A home left group/other-accessible by an older build is tightened to
    owner-only in place."""
    home = tmp_path / "wide_home"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setenv("NAURO_HOME", str(home))
    registry._ensure_nauro_home()
    assert (home.stat().st_mode & 0o077) == 0
