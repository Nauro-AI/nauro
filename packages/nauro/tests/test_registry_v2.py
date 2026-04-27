"""Tests for the v2 registry loader/writer in nauro.store.registry."""

from __future__ import annotations

import json

import pytest

from nauro.constants import (
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V2,
)
from nauro.store import registry
from nauro.store.registry import (
    RegistrySchemaError,
    load_registry_v2,
    save_registry_v2,
)


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


def test_load_v2_empty_when_missing(tmp_path, monkeypatch):
    """Missing registry.json → empty v2 shape."""
    _patch_home(monkeypatch, tmp_path)
    data = load_registry_v2()
    assert data == {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION_V2}


def test_v2_round_trip(tmp_path, monkeypatch):
    """A v2 registry round-trips through save/load with id-keyed entries."""
    _patch_home(monkeypatch, tmp_path)
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
    _patch_home(monkeypatch, tmp_path)
    save_registry_v2({"projects": {}})
    raw = json.loads((tmp_path / "nauro_home" / REGISTRY_FILENAME).read_text())
    assert raw["schema_version"] == REGISTRY_SCHEMA_VERSION_V2


def test_v2_loader_rejects_v1_with_migration_hint(tmp_path, monkeypatch):
    """A v1 registry on disk surfaces the manual-migration message."""
    _patch_home(monkeypatch, tmp_path)
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
    _patch_home(monkeypatch, tmp_path)
    nauro_home = tmp_path / "nauro_home"
    nauro_home.mkdir()
    (nauro_home / REGISTRY_FILENAME).write_text(json.dumps({"projects": {}, "schema_version": 99}))
    with pytest.raises(RegistrySchemaError) as exc:
        load_registry_v2()
    assert "99" in str(exc.value)


def test_v2_save_rejects_non_v2_schema_version(tmp_path, monkeypatch):
    """save_registry_v2 refuses to write anything other than schema_version=2."""
    _patch_home(monkeypatch, tmp_path)
    with pytest.raises(RegistrySchemaError):
        save_registry_v2({"projects": {}, "schema_version": 1})
