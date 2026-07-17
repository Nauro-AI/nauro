from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.repo_config import save_repo_config
from nauro.store.resolution import RepoResolution, resolve_from_cwd
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()
PID = "01KQ6AZGNA0B3QBF67NBXP3S45"


def _local_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(repo, {"mode": "local", "id": PID, "name": "Pareto"})
    return repo


def test_reconnect_continue_changes_no_persistent_state(tmp_path, monkeypatch):
    repo = _local_repo(tmp_path)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["reconnect"], input="continue\n")

    assert result.exit_code == 0, result.output
    assert "has not been connected on this machine" in result.output
    assert "nauro link --cloud" in result.output
    assert "No changes made" in result.output
    assert registry.get_project_v2(PID) is None


def test_reconnect_locates_and_binds_existing_store(tmp_path, monkeypatch):
    repo = _local_repo(tmp_path)
    store = tmp_path / "external" / PID
    scaffold_project_store("Pareto", store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["reconnect"], input=f"locate\n{store}\n")

    assert result.exit_code == 0, result.output
    assert f"Connected 'Pareto' to {store}" in result.output
    resolved = resolve_from_cwd(repo)
    assert isinstance(resolved, RepoResolution)
    assert resolved.store_path == store


def test_reconnect_cloud_restore_uses_same_binding_service(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(
        repo,
        {
            "mode": "cloud",
            "id": PID,
            "name": "Pareto",
            "server_url": "https://example.test",
        },
    )
    monkeypatch.chdir(repo)
    target = registry.get_store_path_v2(PID)

    def restore(_pid, destination):
        assert _pid == PID
        assert destination == target
        scaffold_project_store("Pareto", destination)
        return destination

    with (
        patch("nauro.cli.commands.reconnect.require_cloud_membership", return_value="Pareto"),
        patch("nauro.cli.commands.reconnect.restore_cloud_store", side_effect=restore),
    ):
        result = runner.invoke(app, ["reconnect"], input="restore\n")

    assert result.exit_code == 0, result.output
    assert "Restored and connected 'Pareto'" in result.output
    assert registry.get_project_v2(PID)["mode"] == "cloud"


def test_reconnect_restore_reconciles_server_side_rename(tmp_path, monkeypatch):
    """A cloud rename between adoption and recovery must not dead-end the
    restore: membership verification makes the cloud name authoritative, so
    reconnect adopts it into the registry and the repo config instead of
    conflicting forever.
    """
    from nauro.store.repo_config import load_repo_config

    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(
        repo,
        {
            "mode": "cloud",
            "id": PID,
            "name": "Pareto",
            "server_url": "https://example.test",
        },
    )
    monkeypatch.chdir(repo)
    target = registry.get_store_path_v2(PID)

    def restore(_pid, destination):
        scaffold_project_store("Pareto-Renamed", destination)
        return destination

    with (
        patch(
            "nauro.cli.commands.reconnect.require_cloud_membership",
            return_value="Pareto-Renamed",
        ),
        patch("nauro.cli.commands.reconnect.restore_cloud_store", side_effect=restore),
    ):
        result = runner.invoke(app, ["reconnect"], input="restore\n")

    assert result.exit_code == 0, result.output
    assert "now named 'Pareto-Renamed'" in result.output
    assert registry.get_project_v2(PID)["name"] == "Pareto-Renamed"
    assert load_repo_config(repo)["name"] == "Pareto-Renamed"
    resolved = resolve_from_cwd(repo)
    assert isinstance(resolved, RepoResolution)
    assert resolved.store_path == target


def test_reconnect_healthy_project_is_silent_about_recovery(tmp_path, monkeypatch):
    repo = _local_repo(tmp_path)
    store = registry.get_store_path_v2(PID)
    scaffold_project_store("Pareto", store)
    registry.bind_project_store_v2(
        project_id=PID,
        name="Pareto",
        mode="local",
        repo_path=repo,
        store_path=store,
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["reconnect"])

    assert result.exit_code == 0, result.output
    assert result.output == f"Already connected to 'Pareto'.\n  Store: {store}\n"


def test_reconnect_without_repo_config_does_not_adopt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reconnect"])

    assert result.exit_code == 1
    assert "No Nauro project config found" in result.output
    assert registry.load_registry_v2()["projects"] == {}
