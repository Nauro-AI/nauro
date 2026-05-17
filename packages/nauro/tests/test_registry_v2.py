"""Tests for the v2 registry loader/writer in nauro.store.registry."""

from __future__ import annotations

import json

import pytest

from nauro.constants import (
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store import registry
from nauro.store.registry import (
    RegistrySchemaError,
    is_cloud_project,
    load_registry_v2,
    register_project,
    register_project_v2,
    save_registry_v2,
)


def test_load_v2_empty_when_missing(tmp_path, monkeypatch):
    """Missing registry.json → empty v2 shape."""
    data = load_registry_v2()
    assert data == {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}


def test_v2_round_trip(tmp_path, monkeypatch):
    """A v2 registry round-trips through save/load with id-keyed entries."""
    data = {
        "projects": {
            "01KQ6AZGNA0B3QBF67NBXP3S45": {
                "name": "nauro",
                "mode": "cloud",
                "server_url": "https://mcp.nauro.ai",
                "repo_paths": ["/tmp/nauro"],
            },
            "01KQ7BZGZA0B3QBF67NBXP3S99": {
                "name": "side-project",
                "mode": "local",
                "repo_paths": ["/tmp/side"],
            },
        },
        "schema_version": REGISTRY_SCHEMA_VERSION_V2,
    }
    save_registry_v2(data)
    loaded = load_registry_v2()
    assert loaded == data


def test_v2_save_stamps_schema_version(tmp_path, monkeypatch):
    """save_registry_v2 stamps schema_version=2 when caller omits it."""
    save_registry_v2({"projects": {}})
    raw = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert raw["schema_version"] == REGISTRY_SCHEMA_VERSION_V2


def test_v2_loader_rejects_v1_with_migration_hint(tmp_path, monkeypatch):
    """A v1 registry on disk surfaces the manual-migration message."""
    # Write a v1 registry via the legacy writer.
    registry.save_registry(
        {"projects": {"oldproj": {"repo_paths": ["/tmp/old"]}}, "schema_version": 1}
    )
    with pytest.raises(RegistrySchemaError) as exc:
        load_registry_v2()
    msg = str(exc.value)
    assert "schema_version 1" in msg
    assert "manual migration" in msg


def test_v2_loader_rejects_unknown_schema_version(tmp_path, monkeypatch):
    """A future schema_version is rejected with an upgrade hint."""
    (tmp_path / REGISTRY_FILENAME).write_text(json.dumps({"projects": {}, "schema_version": 99}))
    with pytest.raises(RegistrySchemaError) as exc:
        load_registry_v2()
    assert "99" in str(exc.value)


def test_v2_save_rejects_non_v2_schema_version(tmp_path, monkeypatch):
    """save_registry_v2 refuses to write anything other than schema_version=2."""
    with pytest.raises(RegistrySchemaError):
        save_registry_v2({"projects": {}, "schema_version": 1})


class TestIsCloudProject:
    """Gate predicate for auto-sync and the status report. The presign
    transport has no path for v1 entries (no server-side ULID) or v2
    local-mode entries (not remote-backed), so both must return False."""

    def test_returns_true_for_v2_cloud(self, tmp_path, monkeypatch):
        pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
        register_project_v2(
            "cloudproj",
            [tmp_path],
            mode=REPO_CONFIG_MODE_CLOUD,
            project_id=pid,
            server_url="https://example.test",
        )
        assert is_cloud_project(pid) is True

    def test_returns_false_for_v2_local(self, tmp_path, monkeypatch):
        pid = "01KQ6AZGNA0B3QBF67NBXP3S46"
        register_project_v2(
            "localproj",
            [tmp_path],
            mode=REPO_CONFIG_MODE_LOCAL,
            project_id=pid,
        )
        assert is_cloud_project(pid) is False

    def test_returns_false_for_missing_entry(self, tmp_path, monkeypatch):
        assert is_cloud_project("01KMISSING00000000000000000") is False

    def test_returns_false_for_v1_name(self, tmp_path, monkeypatch):
        register_project("v1name", [tmp_path])
        assert is_cloud_project("v1name") is False
