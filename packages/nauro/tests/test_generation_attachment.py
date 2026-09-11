from __future__ import annotations

import base64
import json
import socket
import time

import httpx
import pytest
from typer.testing import CliRunner

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
from nauro.cli.main import app
from nauro.mcp import stdio_server
from nauro.store import generation_installation as installation
from nauro.store.config import load_config, save_config
from nauro.store.registry import get_project_entry_v2, get_store_path_v2
from nauro.store.repo_config import repo_config_path
from nauro.store.resolution import resolve_project_binding
from nauro.sync import generation_attachment as attachment
from nauro.sync.generation_connection import attachment_connection, connection_for
from nauro.sync.generation_credentials import AccountRecord
from nauro.sync.generation_refresh import admit_generation_store
from nauro.sync.generation_session import GenerationTransferSession
from tests.test_generation_installation import PROJECT_ID, USER_ID, _projection


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("External network"))
    for key in (
        "NAURO_AUTH0_DOMAIN",
        "NAURO_AUTH0_CLIENT_ID",
        "NAURO_API_URL",
        "NAURO_AUTH0_AUDIENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    save_config(
        {
            "auth0_domain": "issuer.example",
            "auth0_client_id": "client",
            "api_url": "https://mcp.nauro.ai",
            "auth0_audience": "https://mcp.nauro.ai/mcp",
            "auth": {"user_id": "01K44444444444444444444444", "access_token": "wrong-account"},
        }
    )
    connection = attachment_connection(DEFAULT_AUTH_REDIRECT_URI)
    credentials = connection.store()
    with credentials.locked():
        credentials.write(
            AccountRecord(
                revision="a" * 64,
                binding=connection.binding(),
                state="active",
                user_id=USER_ID,
                subject="owner",
                access_token="generation-token",
                refresh_token="refresh",
                expires_at=int(time.time()) + 600,
            )
        )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    projection = _projection({"state.md": b"Committed generation state\n"})
    calls = []
    control = {"status": 200, "role": "owner", "after_download": lambda: None}

    def wire(request):
        calls.append(request)
        if request.url.host == "objects.example":
            control["after_download"]()
            return httpx.Response(200, content=b"Committed generation state\n")
        assert request.headers["Authorization"] == "Bearer generation-token"
        if request.url.path == "/projects":
            return httpx.Response(
                200,
                json={
                    "authority": "generation_owner",
                    "projects": [
                        {
                            "project_id": PROJECT_ID,
                            "name": "Synthetic",
                            "role": control["role"],
                        }
                    ],
                },
            )
        if request.url.path == "/generations/projection":
            return httpx.Response(
                control["status"],
                json={
                    "projection": projection.target.identity.model_dump(),
                    "manifest_base64": base64.b64encode(projection.manifest_json).decode(),
                },
            )
        assert request.url.path == "/generations/presign"
        return httpx.Response(
            200,
            json={
                "projection": projection.target.identity.model_dump(),
                "urls": [{"path": "state.md", "url": "https://objects.example/state"}],
                "expires_at": "2999-12-31T23:59:59Z",
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        attachment.httpx,
        "Client",
        lambda **kw: client_type(transport=httpx.MockTransport(wire), **kw),
    )
    monkeypatch.setattr(installation, "read_active_user_id", lambda: pytest.fail("Legacy account"))
    return repo, connection, credentials, control, calls


def _run(repo):
    return CliRunner().invoke(app, ["attach", PROJECT_ID, "--generation", "--repo", str(repo)])


def test_initial_attach_and_ordinary_generation_admission(hosted):
    repo, connection, _, _, calls = hosted
    config_before = load_config()
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert load_config() == config_before
    binding = resolve_project_binding(PROJECT_ID, None, use_cwd=False)
    assert connection_for(binding, DEFAULT_AUTH_REDIRECT_URI) == connection
    with GenerationTransferSession(binding) as session:
        store = admit_generation_store(binding, actor=USER_ID, session=session)
        assert store.read_file("state.md") == "Committed generation state\n"
    read = CliRunner().invoke(app, ["get-raw-file", "state.md", "--project", PROJECT_ID])
    assert read.exit_code == 0, read.output
    assert (
        json.loads(read.stdout)["read_authority"]["generation_id"] == "01K11111111111111111111111"
    )
    response = stdio_server.get_raw_file("state.md", project_id=PROJECT_ID)
    assert response.isError is False
    assert "Committed generation state" in response.content[0].text
    assert json.loads(repo_config_path(repo).read_text())["id"] == PROJECT_ID
    assert {r.url.path for r in calls} == {
        "/projects",
        "/generations/projection",
        "/generations/presign",
        "/state",
    }
    before = {p: p.read_bytes() for p in binding.store_path.rglob("*") if p.is_file()}
    again = _run(repo)
    assert again.exit_code == 1
    assert "Existing stores and interrupted replica evidence are preserved" in again.output
    assert {p: p.read_bytes() for p in binding.store_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("entry", ["legacy.md", ".replica/authority.json"])
def test_existing_evidence_refuses_before_network(hosted, entry):
    repo, _, _, _, calls = hosted
    evidence = get_store_path_v2(PROJECT_ID) / entry
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"retained evidence")
    result = _run(repo)
    assert result.exit_code == 1
    assert calls == []
    assert evidence.read_bytes() == b"retained evidence"
    assert not repo_config_path(repo).exists()


@pytest.mark.parametrize("change", ["expired", "logout", "actor", "endpoint", "revoked"])
def test_credentials_and_authority_rechecked_after_download(hosted, change):
    repo, _, credentials, control, _ = hosted

    def alter():
        if change == "endpoint":
            save_config({"api_url": "https://other.example"})
        elif change == "revoked":
            control["role"] = "viewer"
        else:
            with credentials.locked():
                record = credentials.read()
                if change == "logout":
                    credentials.write(credentials.empty("logged_out"))
                else:
                    delta = (
                        {"expires_at": 1}
                        if change == "expired"
                        else {"user_id": "01K44444444444444444444444"}
                    )
                    credentials.write(record.model_copy(update=delta))

    control["after_download"] = alter
    result = _run(repo)
    assert result.exit_code == 1
    assert not (get_store_path_v2(PROJECT_ID) / ".replica/authority.json").exists()
    assert get_project_entry_v2(PROJECT_ID) is None
    assert not repo_config_path(repo).exists()


@pytest.mark.parametrize("status", [401, 403, 409])
def test_host_refusal_does_not_install(hosted, status):
    repo, _, _, control, _ = hosted
    control["status"] = status
    result = _run(repo)
    assert result.exit_code == 1
    assert not get_store_path_v2(PROJECT_ID).exists()
    assert not repo_config_path(repo).exists()


@pytest.mark.parametrize("boundary", ["carrier", "pointer", "marker", "barrier"])
def test_interrupted_initial_attachment_retains_evidence_and_refuses_repeat(
    hosted, monkeypatch, boundary
):
    repo, _, _, _, _ = hosted
    original = installation.atomic_write_bytes
    names = {
        "carrier": "authorization-view.json",
        "pointer": "pointer.json",
        "marker": "authority.json",
    }

    def write(path, raw):
        original(path, raw)
        if path.name == names.get(boundary):
            raise KeyboardInterrupt()

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    if boundary == "barrier":
        from nauro.sync import generation_refresh

        monkeypatch.setattr(
            generation_refresh, "sync_file", lambda *a: (_ for _ in ()).throw(OSError("barrier"))
        )
    result = _run(repo)
    assert result.exit_code == (1 if boundary == "barrier" else 130), result.output
    root = get_store_path_v2(PROJECT_ID)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before
    assert not repo_config_path(repo).exists()
    again = _run(repo)
    assert again.exit_code == 1
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("point", ["carrier", "pointer"])
def test_owner_revocation_during_control_publication_stops_marker(hosted, monkeypatch, point):
    repo, _, _, control, _ = hosted
    original = installation.atomic_write_bytes

    def write(path, raw):
        original(path, raw)
        if path.name == {"carrier": "authorization-view.json", "pointer": "pointer.json"}[point]:
            control["role"] = "viewer"

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    result = _run(repo)
    assert result.exit_code == 1
    assert not (get_store_path_v2(PROJECT_ID) / ".replica/authority.json").exists()
    assert not repo_config_path(repo).exists()


def test_reauthentication_before_marker_uses_generation_login(hosted, monkeypatch):
    repo, connection, credentials, _, _ = hosted
    with credentials.locked():
        record = credentials.read()
        credentials.write(credentials.empty("logged_out"))
    logins = []

    def login(auth, present_url):
        assert auth.connection == connection
        assert not get_store_path_v2(PROJECT_ID).exists()
        logins.append(auth.project)
        present_url("https://issuer.example/authorize")
        with credentials.locked():
            credentials.write(record)

    monkeypatch.setattr(attachment.GenerationAuth, "login", login)
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert logins == [PROJECT_ID]


def test_generation_change_before_marker_refuses(hosted, monkeypatch):
    from nauro.sync import generation_acquisition

    repo, _, _, _, _ = hosted
    original = generation_acquisition.check_generation_projection

    def changed(*args, **kwargs):
        target = original(*args, **kwargs)
        from nauro.store.generation_projection import GenerationProjectionTarget

        return GenerationProjectionTarget(
            target.binding,
            target.identity.model_copy(
                update={
                    "generation_id": "01K66666666666666666666666",
                }
            ),
        )

    monkeypatch.setattr(generation_acquisition, "check_generation_projection", changed)
    result = _run(repo)
    assert result.exit_code == 1
    assert not (get_store_path_v2(PROJECT_ID) / ".replica/authority.json").exists()
    assert not repo_config_path(repo).exists()


@pytest.mark.parametrize("change", ["scope", "revision"])
def test_same_actor_context_change_stops_completion(hosted, monkeypatch, change):
    from nauro.store.generation_projection import GenerationProjectionTarget
    from nauro.sync import generation_acquisition

    repo, _, credentials, control, _ = hosted
    if change == "revision":

        def renewed():
            with credentials.locked():
                credentials.write(credentials.read().model_copy(update={"revision": "b" * 64}))

        control["after_download"] = renewed
    else:
        original = generation_acquisition.check_generation_projection

        def changed(*args, **kwargs):
            target = original(*args, **kwargs)
            return GenerationProjectionTarget(
                target.binding,
                target.identity.model_copy(
                    update={
                        "projection_scope_id": "b" * 64,
                    }
                ),
            )

        monkeypatch.setattr(generation_acquisition, "check_generation_projection", changed)
    result = _run(repo)
    assert result.exit_code == 1
    assert not (get_store_path_v2(PROJECT_ID) / ".replica/authority.json").exists()
    assert not repo_config_path(repo).exists()


def test_old_listing_is_not_owner_authorization(hosted, monkeypatch):
    repo, _, _, _, _ = hosted
    original = attachment.response_json

    def legacy(*args, **kwargs):
        response = original(*args, **kwargs)
        response.pop("authority", None)
        return response

    monkeypatch.setattr(attachment, "response_json", legacy)
    result = _run(repo)
    assert result.exit_code == 1
    assert not get_store_path_v2(PROJECT_ID).exists()
    assert not repo_config_path(repo).exists()
