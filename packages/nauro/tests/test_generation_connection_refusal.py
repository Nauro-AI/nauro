import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import get_store_path_v2, register_project_v2
from nauro.store.repo_config import save_repo_config

PID = "01K33333333333333333333333"


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


@pytest.mark.parametrize("controls", ["valid", "corrupt", "empty", "dangling"])
@pytest.mark.parametrize("route", ["attach", "attach_unregistered", "reconnect", "locate"])
def test_legacy_connection_refuses_replica_before_side_effects(
    tmp_path, monkeypatch, controls, route
):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.chdir(repo)
    if route in {"attach", "reconnect"}:
        pid, store = register_project_v2(
            "Replica", [repo], mode="cloud", server_url="https://probe.example"
        )
        save_repo_config(
            repo,
            {"mode": "cloud", "id": pid, "name": "Replica", "server_url": "https://probe.example"},
        )
    else:
        pid = PID
        store = tmp_path / "external" / pid if route == "locate" else get_store_path_v2(pid)
        if route == "locate":
            save_repo_config(repo, {"mode": "local", "id": pid, "name": "Replica"})
    store.mkdir(parents=True, exist_ok=True)
    control = store / ".replica"
    if controls == "dangling":
        control.symlink_to(store / "missing")
    else:
        control.mkdir()
        if controls != "empty":
            marker = GenerationAuthorityMarker(
                schema_version=1, authority="generation", project_id=pid, store_format_version=1
            ).canonical_bytes()
            (control / "authority.json").write_bytes(marker if controls == "valid" else b"broken")
    (store / "project.md").write_text("Preserved legacy evidence")
    (repo / "AGENTS.md").write_text("Preserved guidance")

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy connection side effect ran")

    for module in ("attach", "reconnect"):
        for name in (
            "require_cloud_membership",
            "restore_cloud_store",
            "bind_project_store_v2",
            "warn_then_regen",
        ):
            monkeypatch.setattr(f"nauro.cli.commands.{module}.{name}", forbidden)
    monkeypatch.setattr("nauro.cli.commands.reconnect.bind_local_store", forbidden)
    before = tree(tmp_path)
    command = "attach" if route.startswith("attach") else "reconnect"
    args = [command, pid] if command == "attach" else [command]
    result = CliRunner().invoke(app, args, input=f"locate\n{store}\n")
    assert result.exit_code == 1
    assert f"Error: nauro {command} is unavailable for generation replicas." in result.output
    assert tree(tmp_path) == before
