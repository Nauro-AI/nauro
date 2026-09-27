"""Plain ``nauro attach PROJECT`` routes by local evidence and discovered server authority."""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import typer
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.generation_authority import GenerationAuthorityMarker, RefreshRequiredError
from nauro.store.migration_admission import MigrationAdmissionError
from nauro.store.repo_config import repo_config_path, save_repo_config
from nauro.store.resolution import (
    ResolvedProjectBinding,
    resolve_project_binding,
    resolve_registered_project,
)
from nauro.sync import generation_discovery
from nauro.sync.generation_discovery import AuthorityDiscovery
from nauro.sync.generation_session import GenerationConnectionError
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_generation_attachment import hosted as hosted_fixture
from tests.test_generation_connection_refusal import tree
from tests.test_generation_installation import PROJECT_ID as PID

ORIGIN = "https://mcp.nauro.ai"
GENERATION = AuthorityDiscovery("generation", 200, None)
UNREPORTED = (
    "A saved conversion exists for this project but the server does not report "
    "generation authority; owner recovery is required before this project can be attached."
)
SIGN_IN = "Sign in to attach this generation project:\nhttps://login.example/start"


def _forbidden(*args, **kwargs):
    pytest.fail("The legacy block ran after a generation decision")


@pytest.fixture
def routed(tmp_path, monkeypatch):
    repo = hosted_fixture.__wrapped__(tmp_path, monkeypatch)[0]
    state = {"authority": GENERATION, "result": SimpleNamespace(phase="completed")}
    calls: list[tuple] = []

    def discover(project_id):
        calls.append(("discover", project_id))
        return state["authority"]

    def install(project, repo_path, present):
        calls.append(("install", project, repo_path))
        present("https://login.example/start")
        if isinstance(state["result"], Exception):
            raise state["result"]
        return ResolvedProjectBinding(
            tmp_path / "projects" / project, project, "Synth", "cloud", ORIGIN
        )

    def login(self, present):
        calls.append(("login", self.project))
        present("https://login.example/start")
        if state.get("rebind"):
            save_repo_config(repo, {"mode": "cloud", "id": PID, "name": "x", "server_url": ORIGIN})

    def guided(session, *, emit, confirm):
        calls.append(("guided", session.binding, session.repo))
        answer = confirm("Proceed?")
        calls.append(("answer", answer))
        if not answer:
            (tmp_path / "declined-record").write_text("declined")
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr("nauro.cli.connection_routing.discover_project_authority", discover)
    monkeypatch.setattr("nauro.sync.generation_attachment.attach_generation", install)
    monkeypatch.setattr("nauro.sync.generation_credentials.GenerationAuth.login", login)
    monkeypatch.setattr("nauro.cli.generation_upgrade.guided_existing_hosted_upgrade", guided)
    return repo, state, calls


def _forbid_legacy(monkeypatch):
    for name in (
        "legacy_write_guard",
        "require_cloud_membership",
        "restore_cloud_store",
        "scaffold_empty_store",
        "bind_project_store_v2",
        "warn_then_regen",
    ):
        monkeypatch.setattr(f"nauro.cli.commands.attach.{name}", _forbidden)


def _legacy_marker(monkeypatch):
    reached = []

    @contextmanager
    def guard(store, command):
        reached.append((store, command))
        raise typer.Exit(code=3)
        yield

    monkeypatch.setattr("nauro.cli.commands.attach.legacy_write_guard", guard)
    return reached


def _bound(tmp_path, *, mode="cloud", server_url=ORIGIN, content=True):
    _, store = registry.register_project_v2(
        "team-proj", [tmp_path / "other"], mode=mode, project_id=PID, server_url=server_url
    )
    if content:
        (store / "project.md").write_text("Legacy copy")
    return store


def _run(repo, answer="y\n"):
    return CliRunner().invoke(app, ["attach", PID, "--repo", str(repo)], input=answer)


def _disconnect(tmp_path, state):
    store = _bound(tmp_path)
    shutil.rmtree(store)
    external = tmp_path / "external" / PID
    if state == "invalid":
        store.write_text("Not a store")
    if state == "conflict":
        store.mkdir()
        external.mkdir(parents=True)
        (external / "project.md").write_text("External copy")
    if state == "invalid_external":
        external.mkdir(parents=True)
    if state in {"conflict", "invalid_external"}:
        raw = registry.load_registry_v2()
        raw["projects"][PID]["store_path"] = str(external)
        registry.save_registry_v2(raw)
    connection = resolve_registered_project(PID)
    assert connection.reason_code == {
        "missing": "connected_record_missing",
        "conflict": "connected_binding_conflict",
    }.get(state, "connected_record_invalid")
    return connection


@pytest.mark.parametrize(
    "answer",
    [
        AuthorityDiscovery("legacy", 409, "generation_authority_required"),
        AuthorityDiscovery("legacy", 409, "single_writer_refused"),
        AuthorityDiscovery("legacy", 503, "authority_unavailable"),
        AuthorityDiscovery("legacy", 403, "forbidden"),
        AuthorityDiscovery("legacy", 200, None),
        AuthorityDiscovery("legacy", None, None),
    ],
)
@pytest.mark.parametrize("local", ["absent", "bound"])
def test_legacy_answer_runs_the_legacy_block(routed, tmp_path, monkeypatch, answer, local):
    repo, state, calls = routed
    state["authority"] = answer
    store = _bound(tmp_path) if local == "bound" else tmp_path / "projects" / PID
    reached = _legacy_marker(monkeypatch)
    result = _run(repo)
    assert result.exit_code == 3
    assert reached == [(store, "attach")]
    assert calls == [("discover", PID)]


def test_production_single_writer_answer_runs_the_legacy_block(routed, tmp_path, monkeypatch):
    repo, _, calls = routed
    monkeypatch.setattr(
        "nauro.cli.connection_routing.discover_project_authority",
        generation_discovery.discover_project_authority,
    )
    save_repo_config(
        repo, {"mode": "cloud", "id": PID, "name": "t", "server_url": "https://elsewhere.example"}
    )
    requests = []

    def wire(method, url, **kwargs):
        requests.append((url, kwargs.get("params")))
        request = httpx.Request(method, url)
        if kwargs.get("params"):
            return httpx.Response(409, json={"detail": "single_writer_refused"}, request=request)
        row = {"project_id": PID, "name": "team-proj", "role": "owner", "created_at": "2026"}
        return httpx.Response(200, json=[row], request=request)

    monkeypatch.setattr(generation_discovery.httpx, "request", wire)
    monkeypatch.setattr(
        "nauro.cli.commands.attach.restore_cloud_store",
        lambda pid, destination, reporter: (
            scaffold_project_store("team-proj", destination) or destination
        ),
    )
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert f"Attached 'team-proj' to {repo.resolve()}" in result.output
    assert requests[0] == (ORIGIN + "/projects", {"project_id": PID})
    assert all(url.startswith(ORIGIN) for url, _ in requests)
    assert calls == []


@pytest.mark.parametrize(
    "local",
    [
        "absent",
        "bound_empty",
        "nonempty_unregistered",
        "unreadable_retained_record",
        "bound_retained_record",
        "bound_unreadable_retained_record",
    ],
)
def test_generation_installs_every_other_destination(routed, tmp_path, monkeypatch, local):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    if local == "bound_empty":
        _bound(tmp_path, content=False)
    if local in {"nonempty_unregistered", "unreadable_retained_record"}:
        (tmp_path / "projects" / PID).mkdir(parents=True)
        (tmp_path / "projects" / PID / "project.md").write_text("Unregistered copy")
    if local.startswith("bound_") and local != "bound_empty":
        _bound(tmp_path)
    if local == "bound_retained_record":
        monkeypatch.setattr(
            "nauro.sync.generation_attachment_record.read_record", lambda project: object()
        )
    if local.endswith("unreadable_retained_record"):

        def unreadable(project):
            raise RefreshRequiredError("Retained attachment evidence is invalid.")

        monkeypatch.setattr("nauro.sync.generation_attachment_record.read_record", unreadable)
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert calls == [("discover", PID), ("install", PID, repo)]
    assert SIGN_IN in result.output
    assert f"Attached generation project 'Synth' to {repo.resolve()}" in result.output


@pytest.mark.parametrize("retained", [False, True])
def test_failed_installation_never_reaches_the_legacy_block(
    routed, tmp_path, monkeypatch, retained
):
    repo, state, calls = routed
    _forbid_legacy(monkeypatch)
    if retained:
        _bound(tmp_path)
        monkeypatch.setattr(
            "nauro.sync.generation_attachment_record.read_record", lambda project: object()
        )
    state["result"] = RefreshRequiredError("The saved attachment projection changed.")
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    assert "Attachment incomplete: The saved attachment projection changed." in result.output
    assert calls == [("discover", PID), ("install", PID, repo)]
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ("completed", 0),
        ("declined", 1),
        ("assessed", 1),
        ("blocked", 1),
        ("installing", 1),
        (None, 1),
        (GenerationConnectionError("Current owner access could not be confirmed."), 1),
    ],
)
def test_bound_legacy_copy_enters_the_guided_upgrade(routed, tmp_path, monkeypatch, result, code):
    repo, state, calls = routed
    _forbid_legacy(monkeypatch)
    _bound(tmp_path)
    state["result"] = SimpleNamespace(phase=result) if isinstance(result, str) else result
    binding = resolve_project_binding(PID, None, use_cwd=False)
    before = tree(tmp_path)
    outcome = _run(repo)
    assert outcome.exit_code == code, outcome.output
    assert calls == [("discover", PID), ("guided", binding, repo), ("answer", True)]
    entry = registry.get_project_v2(PID)
    assert (str(repo.resolve()) in entry["repo_paths"]) is (code == 0)
    if code:
        assert not repo_config_path(repo).exists()
        assert tree(tmp_path) == before
    else:
        expected = tmp_path / "expected"
        expected.mkdir()
        fields = {"mode": "cloud", "id": PID, "name": "team-proj", "server_url": ORIGIN}
        save_repo_config(expected, fields)
        written = repo_config_path(repo).read_bytes()
        assert written == repo_config_path(expected).read_bytes()
    if isinstance(result, Exception):
        assert "Upgrade incomplete: Current owner access could not be confirmed." in outcome.output


@pytest.mark.parametrize("rebind", [False, True])
def test_upgrade_logs_in_before_the_session(routed, tmp_path, monkeypatch, rebind):
    repo, state, calls = routed
    _forbid_legacy(monkeypatch)
    _bound(tmp_path)
    state["rebind"] = rebind
    monkeypatch.setattr(
        "nauro.sync.generation_credentials.GenerationAuth.status", lambda s: "expired"
    )
    result = _run(repo)
    assert SIGN_IN in result.output
    if rebind:
        assert result.exit_code == 1
        assert "Upgrade incomplete: The project association changed during login." in result.output
        assert calls == [("discover", PID), ("login", PID)]
    else:
        assert result.exit_code == 0, result.output
        assert [call[0] for call in calls] == ["discover", "login", "guided", "answer"]


@pytest.mark.parametrize("interrupt", ["eof", "abort"])
def test_interrupted_consent_records_no_disposition(routed, tmp_path, monkeypatch, interrupt):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    _bound(tmp_path)
    if interrupt == "abort":

        def interrupted(*args, **kwargs):
            raise typer.Abort()

        monkeypatch.setattr(typer, "confirm", interrupted)
    before = tree(tmp_path)
    result = _run(repo, answer="" if interrupt == "eof" else "y\n")
    assert result.exit_code == 1
    binding = resolve_project_binding(PID, None, use_cwd=False)
    assert calls == [("discover", PID), ("guided", binding, repo)]
    assert "Aborted!" in result.output
    assert not (tmp_path / "declined-record").exists()
    assert tree(tmp_path) == before


def test_upgrade_binding_ignores_the_invoking_repo_config(routed, tmp_path, monkeypatch):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    _bound(tmp_path)
    save_repo_config(repo, {"mode": "cloud", "id": PID, "name": "renamed", "server_url": ORIGIN})
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert calls[1] == ("guided", resolve_project_binding(PID, None, use_cwd=False), repo)


def test_replica_check_error_keeps_the_legacy_refusal(routed, tmp_path, monkeypatch):
    repo, _, calls = routed
    _bound(tmp_path)
    lstat = Path.lstat

    def unverifiable(path):
        if path.name == ".replica":
            raise PermissionError("denied")
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", unverifiable)
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    assert "Error: Cannot verify project write authority." in result.output
    assert calls == []
    assert tree(tmp_path) == before


def test_unreadable_bound_store_refuses_instead_of_installing(routed, tmp_path, monkeypatch):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    store = _bound(tmp_path)
    store.chmod(0o300)
    try:
        result = _run(repo)
    finally:
        store.chmod(0o755)
    assert result.exit_code == 1
    assert "Error: Cannot verify project write authority." in result.output
    assert calls == [("discover", PID)]


@pytest.mark.parametrize("state", ["missing", "invalid", "conflict", "invalid_external"])
def test_saved_conversion_on_a_disconnected_project_refuses_without_discovery(
    routed, tmp_path, monkeypatch, state
):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    connection = _disconnect(tmp_path, state)
    inspected = []
    monkeypatch.setattr(
        "nauro.cli.connection_routing.inspect_migration",
        lambda path: inspected.append(path) or object(),
    )
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    assert f"Error: {connection.guidance}" in result.output
    assert inspected == [connection.store_path]
    assert calls == []
    assert tree(tmp_path) == before


@pytest.mark.parametrize("record", [False, True])
def test_saved_conversion_without_generation_authority_refuses(
    routed, tmp_path, monkeypatch, record
):
    repo, state, calls = routed
    _forbid_legacy(monkeypatch)
    _bound(tmp_path)
    state["authority"] = AuthorityDiscovery("legacy", 409, "generation_authority_required")

    def inspect(path):
        if not record:
            raise MigrationAdmissionError(
                "Migration evidence is unavailable; inspect setup recovery."
            )
        return object()

    monkeypatch.setattr("nauro.cli.connection_routing.inspect_migration", inspect)
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    if record:
        assert UNREPORTED in result.output
        assert calls == [("discover", PID)]
    else:
        assert "Error: Migration evidence is unavailable; inspect setup recovery." in result.output
        assert calls == []
    assert tree(tmp_path) == before


@pytest.mark.parametrize("credential", ["active", "expired"])
@pytest.mark.parametrize(
    ("mode", "server_url", "content"),
    [
        ("local", None, True),
        ("cloud", "https://other.example", True),
        ("cloud", "https://other.example", False),
        ("cloud", ORIGIN + "/", True),
    ],
)
def test_registered_connection_to_another_server_refuses_before_login(
    routed, tmp_path, monkeypatch, mode, server_url, content, credential
):
    repo, _, calls = routed
    _forbid_legacy(monkeypatch)
    if credential == "expired":
        monkeypatch.setattr(
            "nauro.sync.generation_credentials.GenerationAuth.status", lambda s: "expired"
        )
    _bound(tmp_path, mode=mode, server_url=server_url, content=content)
    if mode == "local":
        raw = registry.load_registry_v2()
        raw["projects"][PID]["server_url"] = ORIGIN
        registry.save_registry_v2(raw)
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    assert (
        "The registered connection for this project uses another server; "
        "owner recovery is required." in result.output
    )
    assert calls == [("discover", PID)]
    assert tree(tmp_path) == before


@pytest.mark.parametrize("state", ["missing", "invalid", "conflict", "invalid_external"])
def test_disconnected_states_run_the_legacy_block_without_discovery(
    routed, tmp_path, monkeypatch, state
):
    repo, _, calls = routed
    connection = _disconnect(tmp_path, state)
    reached = _legacy_marker(monkeypatch)
    assert _run(repo).exit_code == 3
    assert reached == [(connection.store_path, "attach")]
    assert calls == []


@pytest.mark.parametrize("controls", ["valid", "corrupt", "empty", "dangling"])
@pytest.mark.parametrize("registered", [True, False])
def test_replica_keeps_the_refusal_without_discovery(routed, tmp_path, controls, registered):
    repo, _, calls = routed
    store = _bound(tmp_path) if registered else tmp_path / "projects" / PID
    store.mkdir(parents=True, exist_ok=True)
    control = store / ".replica"
    if controls == "dangling":
        control.symlink_to(store / "missing")
    else:
        control.mkdir()
        if controls != "empty":
            marker = GenerationAuthorityMarker(
                schema_version=1, authority="generation", project_id=PID, store_format_version=1
            ).canonical_bytes()
            (control / "authority.json").write_bytes(marker if controls == "valid" else b"broken")
    before = tree(tmp_path)
    result = _run(repo)
    assert result.exit_code == 1
    assert "Error: nauro attach is unavailable for generation replicas." in result.output
    assert calls == []
    assert tree(tmp_path) == before


def test_routing_module_defers_every_generation_import():
    code = (
        "import sys, nauro.cli.connection_routing\n"
        "print(sorted(m for m in sys.modules if m.startswith('nauro') and any(part in m for part "
        "in ('credential', 'generation_attachment', 'generation_upgrade', 'migration_'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "['nauro.store.migration_admission']"
