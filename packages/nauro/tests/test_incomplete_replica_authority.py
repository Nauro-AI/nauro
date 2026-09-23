import socket
from importlib import import_module

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import stdio_server
from nauro.store.generation_authority import GenerationControlCorruptError
from nauro.store.read_authority import observe_generation_marker
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.store.resolution import resolve_project_binding


@pytest.fixture(params=["empty", "intent", "root"])
def incomplete(request, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    project, store = register_project_v2(
        "Incomplete", [repo], mode="cloud", server_url="https://synthetic.example"
    )
    store.mkdir(parents=True, exist_ok=True)
    save_repo_config(
        repo,
        {
            "mode": "cloud",
            "id": project,
            "name": "Incomplete",
            "server_url": "https://synthetic.example",
        },
    )
    monkeypatch.chdir(repo)
    control = store / ".replica"
    control.mkdir()
    if request.param == "intent":
        (control / "refresh-intent.json").write_bytes(b"retained interruption evidence")
    elif request.param == "root":
        (control / "retained-root").mkdir()
        (control / "retained-root" / "state.md").write_bytes(b"retained bytes")
    (store / "state.md").write_bytes(b"must not serve legacy state")

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy access or network attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for module, names in [
        ("nauro.sync.hooks", ["pull_before_session"]),
        (
            "nauro.cli.commands.sync",
            ["capture_snapshot", "_pull_from_cloud", "push_store_to_cloud"],
        ),
        ("nauro.mcp.tools", ["tool_get_raw_file", "tool_propose_decision"]),
        (
            "nauro.mcp.stdio_server",
            ["tool_propose_decision", "tool_flag_question", "tool_update_state"],
        ),
    ]:
        for name in names:
            monkeypatch.setattr(import_module(module), name, forbidden)
    before = {
        str(p.relative_to(store)): p.read_bytes() if p.is_file() else None for p in store.rglob("*")
    }
    yield project, store, repo
    assert {
        str(p.relative_to(store)): p.read_bytes() if p.is_file() else None for p in store.rglob("*")
    } == before


def test_missing_marker_with_controls_is_not_legacy(incomplete):
    project, _, _ = incomplete
    binding = resolve_project_binding(project, None, use_cwd=False)
    with pytest.raises(GenerationControlCorruptError, match="incomplete"):
        observe_generation_marker(binding)


@pytest.mark.parametrize(
    "args", [["get-raw-file", "state.md"], ["sync"], ["propose-decision", "Synthetic rationale"]]
)
def test_ordinary_cli_refuses_incomplete_controls(incomplete, args):
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    if args[0] == "propose-decision":
        assert result.output == (
            "No verified result. Check auth status or log in, then discover or recover "
            "in this project. No retry was sent.\n"
        )
    else:
        assert result.stderr == (
            "Error: Generation replica controls are incomplete; legacy fallback is unavailable.\n"
        )


def test_stdio_read_and_startup_refuse_fallback(incomplete, caplog):
    project, _, _ = incomplete
    result = stdio_server.get_raw_file("state.md", project_id=project)
    assert result.isError is True
    assert result.content[0].text == "Error: Project read authority is unavailable."
    stdio_server._pull_on_startup()
    assert "session-start pull: unavailable" in caplog.text


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("propose_decision", {"rationale": "Synthetic rationale"}),
        ("flag_question", {"question": "Synthetic question?"}),
        ("update_state", {"delta": "Synthetic state"}),
    ],
)
def test_stdio_writes_refuse_before_legacy_adapter(incomplete, name, arguments):
    project, _, _ = incomplete
    with pytest.raises(GenerationControlCorruptError, match="incomplete"):
        getattr(stdio_server, name)(project_id=project, **arguments)


def test_no_replica_controls_preserves_legacy_selection(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project, store = register_project_v2("Legacy", [repo])
    store.mkdir(parents=True, exist_ok=True)
    binding = resolve_project_binding(project, None, use_cwd=False)
    assert observe_generation_marker(binding) is None
    assert list(store.iterdir()) == []
