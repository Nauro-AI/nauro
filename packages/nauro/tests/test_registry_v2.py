"""Tests for the v2 registry loader/writer in nauro.store.registry."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nauro.constants import (
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_MODE_CLOUD,
    REPO_CONFIG_MODE_LOCAL,
)
from nauro.store import registry
from nauro.store.registry import (
    RegistryEntryV2,
    RegistrySchemaError,
    StoreBindingError,
    bind_project_store_v2,
    is_cloud_project,
    load_registry_v2,
    register_project,
    register_project_v2,
    resolve_registered_store_path_v2,
    save_registry_v2,
    validate_registry_entry_v2,
)
from nauro.templates.scaffolds import scaffold_project_store


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


def test_v2_round_trip_preserves_optional_external_store_path(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    store_path = tmp_path / "external" / pid
    data = {
        "projects": {
            pid: {
                "name": "nauro",
                "mode": "local",
                "repo_paths": [str(tmp_path / "repo")],
                "store_path": str(store_path),
            }
        },
        "schema_version": REGISTRY_SCHEMA_VERSION_V2,
    }

    save_registry_v2(data)

    assert load_registry_v2() == data


def test_registry_entry_boundary_normalizes_lists_without_changing_json_shape():
    raw = {
        "name": "nauro",
        "mode": "local",
        "repo_paths": ["/work/nauro"],
        "future_field": {"enabled": True},
    }

    entry = validate_registry_entry_v2("01KQ6AZGNA0B3QBF67NBXP3S45", raw)

    assert isinstance(entry, RegistryEntryV2)
    assert entry.repo_paths == ("/work/nauro",)
    assert entry.model_dump(mode="json", exclude_unset=True) == raw


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "remote"),
        ("repo_paths", [42]),
        ("store_path", 42),
    ],
)
def test_registry_entry_boundary_rejects_malformed_typed_fields(field, value):
    raw = {
        "name": "nauro",
        "mode": "local",
        "repo_paths": ["/work/nauro"],
        field: value,
    }

    with pytest.raises(StoreBindingError) as exc:
        validate_registry_entry_v2("01KQ6AZGNA0B3QBF67NBXP3S45", raw)

    assert exc.value.reason_code == "connected_record_invalid"


def test_bind_external_store_records_validated_absolute_path(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = tmp_path / "external" / pid
    scaffold_project_store("nauro", store_path)

    bound = bind_project_store_v2(
        project_id=pid,
        name="nauro",
        mode="local",
        repo_path=repo,
        store_path=store_path,
    )

    assert bound == store_path
    entry = load_registry_v2()["projects"][pid]
    assert entry["store_path"] == str(store_path)
    assert entry["repo_paths"] == [str(repo.resolve())]
    assert resolve_registered_store_path_v2(pid) == store_path


def test_bind_default_store_omits_optional_store_path(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = registry.get_store_path_v2(pid)
    scaffold_project_store("nauro", store_path)

    bind_project_store_v2(
        project_id=pid,
        name="nauro",
        mode="local",
        repo_path=repo,
        store_path=store_path,
    )

    assert "store_path" not in load_registry_v2()["projects"][pid]


def test_bind_update_name_reconciles_rename(tmp_path, monkeypatch):
    """A caller holding the authoritative current name (cloud membership)
    reconciles a rename into the registry instead of conflicting."""
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = registry.get_store_path_v2(pid)
    scaffold_project_store("old-name", store_path)
    bind_project_store_v2(
        project_id=pid, name="old-name", mode="local", repo_path=repo, store_path=store_path
    )

    bind_project_store_v2(
        project_id=pid,
        name="new-name",
        mode="local",
        repo_path=repo,
        store_path=store_path,
        update_name=True,
    )

    assert load_registry_v2()["projects"][pid]["name"] == "new-name"


def test_bind_without_update_name_conflicts_on_rename(tmp_path, monkeypatch):
    """A name that only comes from repository config never overwrites
    registry identity — the mismatch stays a typed binding conflict."""
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = registry.get_store_path_v2(pid)
    scaffold_project_store("old-name", store_path)
    bind_project_store_v2(
        project_id=pid, name="old-name", mode="local", repo_path=repo, store_path=store_path
    )

    with pytest.raises(StoreBindingError) as excinfo:
        bind_project_store_v2(
            project_id=pid,
            name="new-name",
            mode="local",
            repo_path=repo,
            store_path=store_path,
        )
    assert excinfo.value.reason_code == "connected_binding_conflict"
    assert load_registry_v2()["projects"][pid]["name"] == "old-name"


def test_bind_default_store_tolerates_incomplete_structure(tmp_path, monkeypatch):
    """Binding the Nauro-managed default path requires existence, not full
    structure, so first connection to an empty cloud record can bind the
    empty mirror directory that sync will populate. External paths keep the
    strict rule (covered by the external-store tests)."""
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = registry.get_store_path_v2(pid)
    store_path.mkdir(parents=True)

    bound = bind_project_store_v2(
        project_id=pid, name="nauro", mode="local", repo_path=repo, store_path=store_path
    )

    assert bound == store_path
    assert load_registry_v2()["projects"][pid]["name"] == "nauro"


def test_bind_external_store_refuses_conflicting_default_store(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    scaffold_project_store("default", registry.get_store_path_v2(pid))
    external = tmp_path / "external" / pid
    scaffold_project_store("external", external)

    with pytest.raises(StoreBindingError) as exc:
        bind_project_store_v2(
            project_id=pid,
            name="nauro",
            mode="local",
            repo_path=repo,
            store_path=external,
        )

    assert exc.value.reason_code == "connected_binding_conflict"
    assert load_registry_v2()["projects"] == {}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_bind_external_store_refuses_symlink_component(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    real_parent = tmp_path / "real"
    store_path = real_parent / pid
    scaffold_project_store("nauro", store_path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(StoreBindingError) as exc:
        bind_project_store_v2(
            project_id=pid,
            name="nauro",
            mode="local",
            repo_path=repo,
            store_path=Path(linked_parent / pid),
        )

    assert exc.value.reason_code == "connected_record_invalid"


def test_bind_external_store_refuses_wrong_identity_basename(tmp_path, monkeypatch):
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo = tmp_path / "repo"
    repo.mkdir()
    store_path = tmp_path / "external" / "different-id"
    scaffold_project_store("nauro", store_path)

    with pytest.raises(StoreBindingError) as exc:
        bind_project_store_v2(
            project_id=pid,
            name="nauro",
            mode="local",
            repo_path=repo,
            store_path=store_path,
        )

    assert exc.value.reason_code == "connected_record_invalid"


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
