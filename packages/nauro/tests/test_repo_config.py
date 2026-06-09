"""Tests for nauro.store.repo_config — repo-local .nauro/config.json + walk-up."""

from __future__ import annotations

import json
import logging

import pytest

from nauro.constants import (
    REPO_CONFIG_DIR,
    REPO_CONFIG_FILENAME,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.store.repo_config import (
    RepoConfigLocationError,
    RepoConfigSchemaError,
    collides_with_global_config,
    find_repo_config,
    generate_ulid,
    load_repo_config,
    repo_config_path,
    save_repo_config,
)

# ── ULID generator ────────────────────────────────────────────────────────────


def test_generate_ulid_shape():
    """A ULID is 26 chars of Crockford base32; no I, L, O, U in the alphabet."""
    ulid = generate_ulid()
    assert len(ulid) == 26
    forbidden = set("ILOU")
    assert not (set(ulid) & forbidden)


def test_generate_ulid_uniqueness():
    """Different invocations produce different ULIDs."""
    ulids = {generate_ulid() for _ in range(50)}
    assert len(ulids) == 50


# ── Local-mode round-trip ─────────────────────────────────────────────────────


def test_local_config_round_trip(tmp_path):
    """Writing then reading a local-mode config preserves every field."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_in = {
        "mode": "local",
        "id": generate_ulid(),
        "name": "myproj",
        "schema_version": REPO_CONFIG_SCHEMA_VERSION,
    }
    written_at = save_repo_config(repo, cfg_in)
    assert written_at == repo / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME

    cfg_out = load_repo_config(repo)
    assert cfg_out == cfg_in


# ── Cloud-mode round-trip ─────────────────────────────────────────────────────


def test_cloud_config_round_trip(tmp_path):
    """Cloud-mode config retains server_url across save/load."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_in = {
        "mode": "cloud",
        "id": "01KQ6AZGNA0B3QBF67NBXP3S45",
        "name": "nauro",
        "server_url": "https://mcp.nauro.ai",
        "schema_version": REPO_CONFIG_SCHEMA_VERSION,
    }
    save_repo_config(repo, cfg_in)
    cfg_out = load_repo_config(repo)
    assert cfg_out == cfg_in


# ── Loader rejection paths ────────────────────────────────────────────────────


def test_loader_rejects_unknown_schema_version(tmp_path):
    """schema_version=99 raises a clear error pointing toward an upgrade path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_dir = repo / REPO_CONFIG_DIR
    cfg_dir.mkdir()
    (cfg_dir / REPO_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "mode": "local",
                "id": generate_ulid(),
                "name": "x",
                "schema_version": 99,
            }
        )
    )
    with pytest.raises(RepoConfigSchemaError) as exc:
        load_repo_config(repo)
    assert "schema_version" in str(exc.value)


def test_loader_remaps_corrupt_json_to_schema_error(tmp_path, caplog):
    """Truncated/invalid JSON raises RepoConfigSchemaError, not a bare
    JSONDecodeError, so callers catching the typed config-error family
    degrade gracefully. The chained cause preserves the underlying parse
    error, and a warning breadcrumb names the path so corruption is not
    silently swallowed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_dir = repo / REPO_CONFIG_DIR
    cfg_dir.mkdir()
    config_file = cfg_dir / REPO_CONFIG_FILENAME
    config_file.write_text('{"mode": "local", "id": "01')  # truncated mid-value

    with caplog.at_level(logging.WARNING, logger="nauro.repo_config"):
        with pytest.raises(RepoConfigSchemaError) as exc:
            load_repo_config(repo)

    assert isinstance(exc.value.__cause__, json.JSONDecodeError)
    assert str(config_file) in caplog.text


def test_save_rejects_invalid_mode(tmp_path):
    """Writing an unknown mode is refused before disk is touched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RepoConfigSchemaError):
        save_repo_config(
            repo,
            {"mode": "magic", "id": generate_ulid(), "name": "x"},
        )
    assert not repo_config_path(repo).exists()


def test_save_rejects_cloud_without_server_url(tmp_path):
    """Cloud mode without server_url is rejected up front."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RepoConfigSchemaError):
        save_repo_config(
            repo,
            {"mode": "cloud", "id": generate_ulid(), "name": "x"},
        )


# ── id validation (path-traversal trust boundary) ─────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../../etc",  # relative traversal
        "../" * 8 + "etc/passwd",  # deep traversal
        "/etc/passwd",  # absolute path
        "a/b",  # nested separator
        "01KQ6AZGNA0B3QBF67NBXP3S4",  # 25 chars — too short
        "01KQ6AZGNA0B3QBF67NBXP3S455",  # 27 chars — too long
        "01KQ6AZGNA0B3QBF67NBXP3S4I",  # 'I' is not in the Crockford alphabet
        "01kq6azgna0b3qbf67nbxp3s45",  # lowercase — not the minted form
    ],
)
def test_validate_rejects_non_ulid_id(tmp_path, bad_id):
    """A repo config whose ``id`` is not a canonical ULID is refused.

    The ``id`` becomes a directory component under ``~/.nauro/projects/``, so
    rejecting traversal/absolute/garbage values at the loader boundary is what
    stops a cloned repo from relocating the store onto arbitrary filesystem
    paths (and thereby reading/writing files outside it).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_dir = repo / REPO_CONFIG_DIR
    cfg_dir.mkdir()
    (cfg_dir / REPO_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "mode": "local",
                "id": bad_id,
                "name": "x",
                "schema_version": REPO_CONFIG_SCHEMA_VERSION,
            }
        )
    )
    with pytest.raises(RepoConfigSchemaError) as exc:
        load_repo_config(repo)
    assert "ULID" in str(exc.value)


def test_save_rejects_non_ulid_id(tmp_path):
    """The writer refuses a malformed id before any bytes touch disk."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RepoConfigSchemaError):
        save_repo_config(repo, {"mode": "local", "id": "../../etc", "name": "x"})
    assert not repo_config_path(repo).exists()


def test_validate_accepts_minted_and_server_ulids(tmp_path):
    """Both a CLI-minted ULID and a representative server-minted ULID validate,
    so the guard does not regress legitimate local or cloud configs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for ulid in (generate_ulid(), "01KQ6AZGNA0B3QBF67NBXP3S45"):
        save_repo_config(repo, {"mode": "local", "id": ulid, "name": "x"})
        assert load_repo_config(repo)["id"] == ulid


# ── find_repo_config walk-up ──────────────────────────────────────────────────


def _seed_config(repo: object) -> object:
    """Helper: write a minimal valid local-mode config into a repo dir."""
    save_repo_config(
        repo,
        {
            "mode": "local",
            "id": generate_ulid(),
            "name": repo.name,
        },
    )
    return repo_config_path(repo)


def test_find_repo_config_at_root(tmp_path):
    """find_repo_config from the repo root returns the config path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _seed_config(repo)
    found = find_repo_config(start=repo)
    assert found == config_path


def test_find_repo_config_walks_up_from_nested_dir(tmp_path):
    """find_repo_config from a nested subdirectory walks up to the root."""
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg" / "deep"
    nested.mkdir(parents=True)
    config_path = _seed_config(repo)
    found = find_repo_config(start=nested)
    assert found == config_path


def test_find_repo_config_returns_none_when_absent(tmp_path):
    """When no .nauro/config.json exists above start, returns None."""
    isolated = tmp_path / "no_repo"
    isolated.mkdir()
    assert find_repo_config(start=isolated) is None


def test_find_repo_config_stops_at_filesystem_root(tmp_path, monkeypatch):
    """The walk-up terminates at the filesystem root (no infinite loop)."""
    # Anchor the walk at tmp_path (which has no config above it) and ensure
    # the call returns rather than spinning. A pytest-imposed timeout would
    # surface infinite loops, but explicit termination is the assertion.
    isolated = tmp_path / "deep" / "tree"
    isolated.mkdir(parents=True)
    # No config anywhere on the path; expect None and no hang.
    assert find_repo_config(start=isolated) is None


def test_find_repo_config_defaults_to_cwd(tmp_path, monkeypatch):
    """Omitting start uses the current working directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _seed_config(repo)
    monkeypatch.chdir(repo)
    found = find_repo_config()
    assert found == config_path


# ── global-config collision guard ─────────────────────────────────────────────


def test_save_refuses_global_config_path(tmp_path, monkeypatch):
    """A repo root whose config path is the global config is refused.

    Replicates the production default layout, where the global config lives
    at ``<home>/.nauro/config.json``: a repo config for ``<home>`` resolves to
    the same file, and writing it would replace auth tokens and telemetry
    consent.
    """
    home = tmp_path / "home"
    nauro_home = home / ".nauro"
    nauro_home.mkdir(parents=True)
    monkeypatch.setenv("NAURO_HOME", str(nauro_home))
    global_config = nauro_home / "config.json"
    sentinel = '{"auth": {"access_token": "keep-me"}}\n'
    global_config.write_text(sentinel)

    assert collides_with_global_config(home)
    with pytest.raises(RepoConfigLocationError):
        save_repo_config(
            home,
            {"mode": "local", "id": generate_ulid(), "name": "demo-project"},
        )

    assert global_config.read_text() == sentinel


def test_collision_respects_nauro_home_override(tmp_path):
    """A ``<dir>/.nauro`` that is not the configured home is a valid repo root.

    The conftest points NAURO_HOME at ``tmp_path``, so ``home-lookalike`` is
    an ordinary directory here; only the actual global config path collides.
    """
    repo = tmp_path / "home-lookalike"
    repo.mkdir()

    assert not collides_with_global_config(repo)
    written = save_repo_config(
        repo,
        {"mode": "local", "id": generate_ulid(), "name": "demo-project"},
    )
    assert written == repo_config_path(repo)
    assert written.is_file()
