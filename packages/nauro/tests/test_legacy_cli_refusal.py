from importlib import import_module

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import tools
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config


@pytest.mark.parametrize(
    "command",
    [
        "flag-question",
        "resolve-question",
        "update-state",
        "import-adr",
        "import-memory-bank",
        "questions-migrate",
        "questions-dry-run",
    ],
)
@pytest.mark.parametrize("control", ["valid", "corrupt", "incomplete", "dangling"])
@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_cli_refuses_before_access(tmp_path, monkeypatch, command, control, explicit):
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
            raw = GenerationAuthorityMarker(
                schema_version=1, authority="generation", project_id=project, store_format_version=1
            ).canonical_bytes()
            (replica / "authority.json").write_bytes(raw if control == "valid" else b"bad")
    (store / "project.md").write_text("Preserved synthetic project\n")
    (repo / "AGENTS.md").write_text("Preserved guidance\n")
    source = tmp_path / "source"
    source.mkdir()
    (source / "projectBrief.md").write_text("Synthetic import\n")

    def snapshot(root):
        return {
            str(p.relative_to(root)): ("link", str(p.readlink()))
            if p.is_symlink()
            else ("file", p.read_bytes())
            if p.is_file()
            else ("dir", None)
            for p in root.rglob("*")
        }

    before = snapshot(store), snapshot(repo)

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy adapter reached")

    for name in ("tool_flag_question", "tool_update_state"):
        monkeypatch.setattr(tools, name, forbidden)
    importer = import_module("nauro.cli.commands.import_cmd")
    for name in ("_import_memory_bank", "_import_adrs", "run_post_commit"):
        monkeypatch.setattr(importer, name, forbidden)
    questions = import_module("nauro.cli.commands.questions")
    monkeypatch.setattr(questions, "FilesystemStore", forbidden)
    monkeypatch.setattr(questions, "run_post_commit", forbidden)
    arguments = {
        "flag-question": ["flag-question", "Synthetic question?"],
        "resolve-question": ["flag-question", "--targets", "Q1", "--resolved-by", "D1"],
        "update-state": ["update-state", "Synthetic state"],
        "import-adr": ["import", "--adr", str(source)],
        "import-memory-bank": ["import", "--memory-bank", str(source)],
        "questions-migrate": ["questions", "migrate"],
        "questions-dry-run": ["questions", "migrate", "--dry-run"],
    }[command]
    label = " ".join(arguments[:2]) if command.startswith("questions-") else arguments[0]
    if explicit:
        arguments += ["--project", "Replica"]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1, result.output
    assert result.stderr == f"Error: nauro {label} is unavailable for generation replicas.\n"
    assert (snapshot(store), snapshot(repo)) == before
