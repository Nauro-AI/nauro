import json
from pathlib import Path

import pytest

from nauro.cli.integrations import orchestrator
from nauro.setup.outcomes import JsonMcpKind


def legacy(repo):
    path = repo / ".cursor/mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"mcpServers": {"nauro": {"command": "/opt/bin/nauro", "args": ["serve", "--stdio"]}}}
        )
    )
    return path


def test_global_write_before_owned_legacy_cleanup(tmp_path):
    path = legacy(tmp_path / "repo")
    outcomes = orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    entry = json.loads((Path.home() / ".cursor/mcp.json").read_text())["mcpServers"]["nauro"]
    assert entry == {"type": "stdio", "command": entry["command"], "args": ["serve", "--stdio"]}
    assert Path(entry["command"]).is_absolute()
    assert not path.exists()
    assert [item.kind for item in outcomes] == [JsonMcpKind.WROTE, JsonMcpKind.REMOVED]


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]", b'{"mcpServers":null}'])
def test_global_failure_preserves_legacy_and_ignore(tmp_path, raw):
    path = legacy(tmp_path / "repo")
    before = path.read_bytes()
    ignore = path.parent.parent / ".gitignore"
    ignore.write_bytes(b"user-content\n")
    target = Path.home() / ".cursor/mcp.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    outcomes = orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    assert len(outcomes) == 1
    assert path.read_bytes() == before
    assert ignore.read_bytes() == b"user-content\n"
    assert target.read_bytes() == raw


@pytest.mark.parametrize("command", ["nauro", "bin/nauro", "/opt/bin/python"])
def test_unsuitable_command_preserves_legacy(tmp_path, monkeypatch, command):
    from nauro.cli import nauro_command

    path = legacy(tmp_path / "repo")
    original = path.read_bytes()
    monkeypatch.setattr(nauro_command, "_interpreter_sibling_candidate", lambda: command)
    monkeypatch.setattr(nauro_command.shutil, "which", lambda name: None)
    outcomes = orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    assert [item.kind for item in outcomes] == [JsonMcpKind.INSTALL_FAILED]
    assert path.read_bytes() == original
    assert not (Path.home() / ".cursor/mcp.json").exists()


@pytest.mark.parametrize("probe", ["probe_nauro_command", "_is_durable_install_path"])
def test_unusable_executable_preserves_legacy(tmp_path, monkeypatch, probe):
    from nauro.cli import nauro_command

    path = legacy(tmp_path / "repo")
    original = path.read_bytes()
    monkeypatch.setattr(nauro_command, probe, lambda *args, **kwargs: False)
    assert (
        orchestrator.cursor_surfaces([path.parent.parent], remove=False)[0].kind
        is JsonMcpKind.INSTALL_FAILED
    )
    assert path.read_bytes() == original


@pytest.mark.parametrize("dangling", [False, True])
def test_global_leaf_symlink_refused(tmp_path, dangling):
    path = legacy(tmp_path / "repo")
    target = tmp_path / "target"
    if not dangling:
        target.write_text("{}")
    global_path = Path.home() / ".cursor/mcp.json"
    global_path.parent.mkdir(parents=True)
    global_path.symlink_to(target)
    assert (
        orchestrator.cursor_surfaces([path.parent.parent], remove=False)[0].kind
        is JsonMcpKind.REFUSED_SYMLINK
    )
    assert path.exists()
    assert global_path.is_symlink()


def test_parent_symlink_and_multiple_repos_write_once(tmp_path, monkeypatch):
    from nauro.cli.integrations import cursor

    repos = [tmp_path / "a", tmp_path / "b"]
    for repo in repos:
        legacy(repo)
    target = tmp_path / "cursor"
    target.mkdir()
    Path.home().mkdir()
    (Path.home() / ".cursor").symlink_to(target, target_is_directory=True)
    calls = []
    write = cursor.write_json_config
    monkeypatch.setattr(
        cursor, "write_json_config", lambda path, data: (calls.append(path), write(path, data))
    )
    outcomes = orchestrator.cursor_surfaces(repos, remove=False)
    assert [item.kind for item in outcomes] == [
        JsonMcpKind.WROTE,
        JsonMcpKind.REMOVED,
        JsonMcpKind.REMOVED,
    ]
    assert calls == [Path.home() / ".cursor/mcp.json"]
    again = orchestrator.cursor_surfaces(repos, remove=False)
    assert [item.kind for item in again] == [
        JsonMcpKind.UNCHANGED,
        JsonMcpKind.NOTHING_TO_REMOVE,
        JsonMcpKind.NOTHING_TO_REMOVE,
    ]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b'{"mcpServers":{"nauro":{"command":"other","args":[]}}}',
        b'{"mcpServers":{"nauro":{"command":"nauro","args":["serve","--stdio"],"env":{}}}}',
    ],
)
def test_uncertain_legacy_preserved(tmp_path, raw):
    path = legacy(tmp_path / "repo")
    path.write_bytes(raw)
    outcomes = orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    assert outcomes[0].kind is JsonMcpKind.WROTE
    assert outcomes[1].kind in {JsonMcpKind.PARSE_ERROR, JsonMcpKind.PRESERVED}
    assert path.read_bytes() == raw


def test_siblings_and_metadata_preserved(tmp_path):
    path = legacy(tmp_path / "repo")
    raw = json.loads(path.read_text())
    raw["metadata"] = {"keep": True}
    raw["mcpServers"]["other"] = {"opaque": [1]}
    path.write_text(json.dumps(raw))
    target = Path.home() / ".cursor/mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"metadata": 42, "mcpServers": {"other": None}}))
    orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    del raw["mcpServers"]["nauro"]
    assert json.loads(path.read_text()) == raw
    global_raw = json.loads(target.read_text())
    del global_raw["mcpServers"]["nauro"]
    assert global_raw == {"metadata": 42, "mcpServers": {"other": None}}


def test_write_failure_preserves_legacy_and_other_cleanup_continues(tmp_path, monkeypatch):
    from nauro.cli.integrations import cursor

    paths = [legacy(tmp_path / name) for name in ("a", "b")]
    originals = [path.read_bytes() for path in paths]
    write = cursor.write_json_config

    def fail_write(path, data):
        raise OSError("write refused")

    monkeypatch.setattr(cursor, "write_json_config", fail_write)
    outcomes = orchestrator.cursor_surfaces([path.parent.parent for path in paths], remove=False)
    assert [item.kind for item in outcomes] == [JsonMcpKind.WRITE_FAILED]
    assert [path.read_bytes() for path in paths] == originals
    monkeypatch.setattr(cursor, "write_json_config", write)
    unlink = Path.unlink

    def fail_first(path, *args, **kwargs):
        if path == paths[0]:
            raise PermissionError("cleanup refused")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first)
    outcomes = orchestrator.cursor_surfaces([path.parent.parent for path in paths], remove=False)
    assert [item.kind for item in outcomes] == [
        JsonMcpKind.WROTE,
        JsonMcpKind.WRITE_FAILED,
        JsonMcpKind.REMOVED,
    ]
    assert paths[0].read_bytes() == originals[0]
    monkeypatch.setattr(Path, "unlink", unlink)
    assert (
        orchestrator.cursor_surfaces([paths[0].parent.parent], remove=False)[1].kind
        is JsonMcpKind.REMOVED
    )


@pytest.mark.parametrize(
    "registry",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"schema_version":1}',
        b'{"schema_version":2,"projects":[]}',
        b'{"schema_version":2,"projects":{"other":{}}}',
    ],
)
def test_uncertain_or_other_project_registry_preserves_global(tmp_path, registry):
    from nauro.store.home import registry_file

    orchestrator.cursor_surfaces([], remove=False)
    target = Path.home() / ".cursor/mcp.json"
    original = target.read_bytes()
    registry_file().write_bytes(registry)
    result = orchestrator.cursor_surfaces([], remove=True, current_project_key="current")
    assert result[0].kind is JsonMcpKind.PRESERVED
    assert target.read_bytes() == original


@pytest.mark.parametrize("override", [None, False, True])
def test_last_project_teardown_and_override(tmp_path, override):
    from nauro.store.home import registry_file

    orchestrator.cursor_surfaces([], remove=False)
    registry_file().write_text(json.dumps({"schema_version": 2, "projects": {"current": {}}}))
    result = orchestrator.cursor_surfaces(
        [], remove=True, current_project_key="current", clear_user_scope_override=override
    )
    assert result[0].kind is (JsonMcpKind.PRESERVED if override is False else JsonMcpKind.REMOVED)


def test_cleanup_removes_only_managed_ignore_and_refuses_repo_symlink(tmp_path):
    import subprocess

    from nauro.setup.git_hygiene import ensure_wiring_ignored

    repo = tmp_path / "repo"
    path = legacy(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ignore = repo / ".gitignore"
    ignore.write_text("user-pattern\n")
    ensure_wiring_ignored(repo, ".cursor/mcp.json")
    orchestrator.cursor_surfaces([repo], remove=False)
    assert not path.exists()
    assert ignore.read_text() == "user-pattern\n"
    path.parent.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path.parent.symlink_to(elsewhere, target_is_directory=True)
    assert orchestrator.cursor_surfaces([repo], remove=False)[1].kind is JsonMcpKind.REFUSED_SYMLINK
    assert list(elsewhere.iterdir()) == []


def test_unreadable_registry_preserves_global(tmp_path, monkeypatch):
    from nauro.cli.integrations import user_scope
    from nauro.store.local_files import UnreadableFileError

    orchestrator.cursor_surfaces([], remove=False)
    target = Path.home() / ".cursor/mcp.json"
    original = target.read_bytes()

    def unreadable(path):
        raise UnreadableFileError(path, "permission denied")

    monkeypatch.setattr(user_scope, "read_text_or_absent", unreadable)
    assert orchestrator.cursor_surfaces([], remove=True)[0].kind is JsonMcpKind.PRESERVED
    assert target.read_bytes() == original


def test_retry_cleans_managed_ignore_after_legacy_file_is_gone(tmp_path):
    import subprocess

    from nauro.setup.git_hygiene import ensure_wiring_ignored

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ensure_wiring_ignored(repo, ".cursor/mcp.json")
    result = orchestrator.cursor_surfaces([repo], remove=False)
    assert result[1].kind is JsonMcpKind.NOTHING_TO_REMOVE
    assert not (repo / ".gitignore").exists()


@pytest.mark.parametrize("alias", [False, True])
def test_home_repository_never_cleans_shared_target(tmp_path, alias):
    home = Path.home()
    home.mkdir()
    repo = home
    if alias:
        repo = tmp_path / "home-alias"
        repo.symlink_to(home, target_is_directory=True)
    result = orchestrator.cursor_surfaces([repo], remove=False)
    target = home / ".cursor/mcp.json"
    assert result[0].kind is JsonMcpKind.WROTE
    original = target.read_bytes()
    result = orchestrator.cursor_surfaces([repo], remove=True, clear_user_scope_override=False)
    assert result[0].kind is JsonMcpKind.PRESERVED
    assert target.read_bytes() == original
    result = orchestrator.cursor_surfaces([repo], remove=True)
    assert [item.kind for item in result].count(JsonMcpKind.REMOVED) == 1
    assert not target.exists()


@pytest.mark.parametrize("sibling", [False, True])
def test_tracked_legacy_requires_explicit_teardown(tmp_path, sibling):
    import subprocess

    repo = tmp_path / "repo"
    path = legacy(repo)
    raw = json.loads(path.read_text())
    if sibling:
        raw["mcpServers"]["other"] = {"command": "other"}
        path.write_text(json.dumps(raw))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", ".cursor/mcp.json"], check=True)
    before = path.read_bytes()
    ignore = repo / ".gitignore"
    ignore.write_bytes(b"user-rule\n")
    result = orchestrator.cursor_surfaces([repo], remove=False)
    assert result[0].kind is JsonMcpKind.WROTE
    assert result[1].kind is JsonMcpKind.REFUSED_TRACKED
    assert path.read_bytes() == before
    assert ignore.read_bytes() == b"user-rule\n"
    result = orchestrator.cursor_surfaces([repo], remove=True)
    assert result[1].kind is JsonMcpKind.REMOVED
    if sibling:
        assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "other"}}}
    else:
        assert not path.exists()
    assert ignore.read_bytes() == b"user-rule\n"


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"command": "wrapper"},
        {"command": "nauro", "args": ["custom"]},
        {"command": "nauro", "args": ["serve", "--stdio"], "env": {"KEEP": "yes"}},
    ],
)
def test_custom_global_entry_preserves_all_bytes_and_legacy(tmp_path, entry):
    path = legacy(tmp_path / "repo")
    before = path.read_bytes()
    target = Path.home() / ".cursor/mcp.json"
    target.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"nauro": entry}, "metadata": "keep"}).encode()
    target.write_bytes(original)
    result = orchestrator.cursor_surfaces([path.parent.parent], remove=False)
    assert [item.kind for item in result] == [JsonMcpKind.PRESERVED]
    assert target.read_bytes() == original
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["cursor", "all"])
def test_failed_install_exits_one_and_shared_preservation_succeeds(tmp_path, monkeypatch, command):
    from typer.testing import CliRunner

    from nauro.cli import nauro_command
    from nauro.cli.main import app
    from nauro.store.registry import register_project_v2
    from nauro.templates.scaffolds import scaffold_project_store

    repo = tmp_path / "repo"
    repo.mkdir()
    _, store = register_project_v2("example", [repo])
    scaffold_project_store("example", store)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda *a, **kw: False)
    result = CliRunner().invoke(app, ["setup", command])
    assert result.exit_code == 1
    assert "durable" in result.output
    assert not (Path.home() / ".cursor/mcp.json").exists()
    other = tmp_path / "other"
    other.mkdir()
    register_project_v2("other", [other])
    result = CliRunner().invoke(app, ["setup", command, "--remove"])
    assert result.exit_code == 0
    assert "preserved" in result.output


@pytest.mark.parametrize(
    "arguments", [["setup", "cursor"], ["setup", "all"], ["adopt", "--name", "example"]]
)
def test_public_setup_preserves_tracked_cursor_wiring(tmp_path, monkeypatch, arguments):
    import subprocess

    from typer.testing import CliRunner

    from nauro.cli.main import app
    from nauro.store.registry import register_project_v2
    from nauro.templates.scaffolds import scaffold_project_store

    repo = tmp_path / "repo"
    target = legacy(repo)
    before = target.read_bytes()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", ".cursor/mcp.json"], check=True)
    monkeypatch.chdir(repo)
    if arguments[0] == "setup":
        _, store = register_project_v2("example", [repo])
        scaffold_project_store("example", store)
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert ".cursor/mcp.json is tracked by git" in result.output
    assert target.read_bytes() == before
    assert (Path.home() / ".cursor/mcp.json").is_file()
