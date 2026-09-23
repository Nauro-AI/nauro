import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


@pytest.mark.parametrize(
    "arguments",
    [
        ["A synthetic decision"],
        ["A synthetic question?"],
        ["A question", "--question"],
        ["A decision?", "--decision"],
    ],
)
@pytest.mark.parametrize("control", ["valid", "corrupt", "incomplete", "dangling"])
@pytest.mark.parametrize("selection", ["cwd", "name", "id"])
def test_note_refuses_replica_evidence_without_mutation(
    tmp_path, monkeypatch, arguments, control, selection
):
    repo = tmp_path / "repo"
    repo.mkdir()
    project, store = register_project_v2(
        "Replica", [repo], mode="cloud", server_url="https://synthetic.example"
    )
    store.mkdir(parents=True, exist_ok=True)
    save_repo_config(
        repo,
        {
            "mode": "cloud",
            "id": project,
            "name": "Replica",
            "server_url": "https://synthetic.example",
        },
    )
    monkeypatch.chdir(repo)
    replica = store / ".replica"
    if control == "dangling":
        replica.symlink_to(tmp_path / "missing", target_is_directory=True)
    else:
        replica.mkdir()
        if control != "incomplete":
            marker = GenerationAuthorityMarker(
                schema_version=1, authority="generation", project_id=project, store_format_version=1
            ).canonical_bytes()
            (replica / "authority.json").write_bytes(marker if control == "valid" else b"bad")
    (store / "project.md").write_text("Preserved synthetic content\n")
    (repo / "AGENTS.md").write_text("Preserved guidance\n")

    def snapshot(root):
        return {
            str(path.relative_to(root)): ("link", str(path.readlink()))
            if path.is_symlink()
            else ("file", path.read_bytes())
            if path.is_file()
            else ("dir", None)
            for path in root.rglob("*")
        }

    before = snapshot(store), snapshot(repo)
    from nauro.cli.commands import note

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy mutation path reached")

    for name in (
        "FilesystemStore",
        "decision_write_lock",
        "store_write_lock",
        "record_event",
        "run_post_commit",
    ):
        monkeypatch.setattr(note, name, forbidden)
    options = (
        [] if selection == "cwd" else ["--project", "Replica" if selection == "name" else project]
    )
    result = CliRunner().invoke(app, ["note", *arguments, *options])
    assert result.exit_code == 1, result.output
    expected = (
        f"Unknown project '{project}'.\nAvailable projects: Replica\n"
        if selection == "id"
        else "Error: nauro note is unavailable for generation replicas.\n"
    )
    assert result.stderr == expected
    assert (snapshot(store), snapshot(repo)) == before


@pytest.mark.parametrize("arguments", [["Synthetic decision"], ["Synthetic question?"]])
def test_note_refuses_unreadable_replica_controls(tmp_path, monkeypatch, arguments):
    from pathlib import Path

    from nauro.cli.commands import note

    repo = tmp_path / "repo"
    repo.mkdir()
    _, store = register_project_v2("Local", [repo])
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo)
    native = Path.lstat

    def inspect(path, *args, **kwargs):
        if path == store / ".replica":
            raise PermissionError("Synthetic denied access")
        return native(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy store constructed")

    monkeypatch.setattr(Path, "lstat", inspect)
    monkeypatch.setattr(note, "FilesystemStore", forbidden)
    result = CliRunner().invoke(app, ["note", *arguments])
    assert result.exit_code == 1
    assert result.stderr == "Error: Cannot verify project write authority.\n"
    assert list(store.iterdir()) == []
