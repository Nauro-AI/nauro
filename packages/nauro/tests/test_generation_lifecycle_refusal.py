import socket

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import get_project_v2, register_project_v2
from nauro.store.repo_config import save_repo_config


def tree(root):
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink()))
            if path.is_symlink()
            else ("dir", None)
            if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
    }


@pytest.fixture(params=["valid", "corrupt", "empty", "dangling"])
def replica(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.chdir(repo)

    def create(mode):
        pid, store = register_project_v2(
            "Replica",
            [repo],
            mode=mode,
            server_url="https://probe.example" if mode == "cloud" else None,
        )
        cfg = {"mode": mode, "id": pid, "name": "Replica"}
        if mode == "cloud":
            cfg["server_url"] = "https://probe.example"
        save_repo_config(repo, cfg)
        store.mkdir(parents=True, exist_ok=True)
        (store / "project.md").write_text("Preserved legacy evidence")
        control = store / ".replica"
        if request.param == "dangling":
            control.symlink_to(store / "missing")
        else:
            control.mkdir()
            if request.param != "empty":
                marker = GenerationAuthorityMarker(
                    schema_version=1, authority="generation", project_id=pid, store_format_version=1
                ).canonical_bytes()
                (control / "authority.json").write_bytes(
                    marker if request.param == "valid" else b"broken"
                )
        return pid, store

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy lifecycle side effect ran")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    return tmp_path, repo, create, forbidden


@pytest.mark.parametrize("mode", ["local", "cloud"])
@pytest.mark.parametrize("route", ["demo", "add_repo", "link", "purge"])
def test_legacy_lifecycle_refuses_before_mutation(replica, monkeypatch, mode, route):
    root, repo, create, forbidden = replica
    _, _ = create(mode)
    extra = root / "extra"
    extra.mkdir()
    for target in (
        "nauro.cli.commands.init.add_repo_v2",
        "nauro.demo.create_demo_project",
        "nauro.cli.commands.link.load_access_token",
        "nauro.cli.commands.link.create_project",
        "nauro.cli.commands.link.rename_project_id_v2",
        "nauro.cli.commands.link.push_changed_files",
        "nauro.cli.commands.adopt.setup_all_surfaces",
    ):
        monkeypatch.setattr(target, forbidden)
    commands = {
        "demo": ["init", "Replica", "--demo", "--force"],
        "add_repo": ["init", "Replica", "--add-repo", str(extra)],
        "link": ["link", "--cloud"],
        "purge": ["adopt", "--remove", "--purge-store", "--yes"],
    }
    before = tree(root)
    result = CliRunner().invoke(app, commands[route])
    assert result.exit_code == 1
    if mode == "local" or route in {"demo", "purge"}:
        assert "is unavailable for generation replicas." in result.output
    assert tree(root) == before


@pytest.mark.parametrize("route", ["unadopt", "registry"])
def test_nonpurging_removal_preserves_replica_evidence(replica, monkeypatch, route):
    _, repo, create, _ = replica
    pid, store = create("cloud")
    before = tree(store)
    monkeypatch.setattr("nauro.cli.commands.adopt.setup_all_surfaces", lambda *args, **kwargs: [])
    args = (
        ["adopt", "--remove", "--yes"] if route == "unadopt" else ["projects", "rm", pid, "--yes"]
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert get_project_v2(pid) is None
    assert tree(store) == before
    assert (repo / ".nauro" / "config.json").exists() is (route == "registry")
