"""Tests for nauro.store.repo_config — repo-local .nauro/config.json + walk-up."""

from __future__ import annotations

import json

import pytest

from nauro.constants import (
    REPO_CONFIG_DIR,
    REPO_CONFIG_FILENAME,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.store.repo_config import (
    RepoConfigSchemaError,
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
