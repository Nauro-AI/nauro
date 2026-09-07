import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.decision_reference import reference_server
from nauro.sync import reference_auth as auth_module
from nauro.sync.decision_profile import (
    RenewalProfile,
    load_reference_profile,
    profile_credentials,
    profile_transport,
)
from nauro.sync.decision_reference_contract import reference_schema
from nauro.sync.reference_auth import ReferenceAuth
from nauro.sync.reference_oauth import callback_code, verify_access

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
ACTOR = "01K" + "0" * 21 + "08"


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    profile = RenewalProfile(
        version=2,
        endpoint="https://probe.example/mcp",
        project_id=PROJECT,
        actor_id=ACTOR,
        credentials_file=str(tmp_path / "credentials.json"),
        issuer="https://issuer.example/",
        client_id="synthetic-client",
        audience="https://probe.example/mcp",
        expected_subject="synthetic-owner",
        redirect_uri="http://127.0.0.1:18765/callback",
    )
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    path.chmod(0o600)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-key"
    calls = []
    control = {"status": 200, "generation": 0}

    def token(**changes):
        claims = {
            "iss": profile.issuer,
            "aud": profile.audience,
            "sub": profile.expected_subject,
            "azp": profile.client_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "scope": "read:context write:context",
        }
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    def wire(request):
        calls.append(request)
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/oauth/token":
            if control["status"] == "lost":
                raise httpx.ReadError("synthetic secret", request=request)
            control["generation"] += 1
            return httpx.Response(
                control["status"],
                json={
                    "access_token": token(jti=str(control["generation"])),
                    "refresh_token": "rotated-" + str(control["generation"]),
                    "token_type": "Bearer",
                },
            )
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "propose_decision", "inputSchema": reference_schema()}]}
        else:
            assert body["params"]["arguments"]["request_mode"] == "discover"
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"version": 1, "requests": [], "next_after": None}),
                    }
                ]
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        auth = ReferenceAuth(profile, client)
        with auth.store.locked():
            auth.store.write(
                auth._record((token(jti="original"), "original-refresh", int(time.time()) + 600))
            )
        yield SimpleNamespace(
            profile=profile,
            path=path,
            token=token,
            auth=auth,
            client=client,
            calls=calls,
            control=control,
        )


def test_renewal_is_visible_to_running_stdio_and_cli_transport(setup):
    transport = profile_transport(setup.profile, setup.client)
    transport.initialize()
    server = reference_server(transport)
    tool = server._tool_manager.get_tool("propose_decision")
    before = profile_credentials(setup.profile).access_token
    setup.auth.refresh()
    after = profile_credentials(setup.profile).access_token
    assert after != before
    response = asyncio.run(tool.run({"project_id": PROJECT, "request_mode": "discover"}))
    assert response.isError is False
    second = profile_transport(load_reference_profile(setup.path), setup.client)
    second.initialize()
    assert second.propose_decision(project_id=PROJECT, request_mode="discover") == {
        "version": 1,
        "requests": [],
        "next_after": None,
    }
    tool_calls = [
        r
        for r in setup.calls
        if r.url.path == "/mcp" and json.loads(r.content)["method"] == "tools/call"
    ]
    assert len(tool_calls) == 2
    assert [r.headers["authorization"] for r in tool_calls] == ["Bearer " + after] * 2


@pytest.mark.parametrize("status", [401, 429, 503, "lost"])
def test_uncertain_or_refused_exchange_never_replays(setup, status):
    setup.control["status"] = status
    with pytest.raises(ValueError, match="login required"):
        setup.auth.refresh()
    assert setup.auth.status() == "reauthentication_required"
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)
    with pytest.raises(ValueError):
        setup.auth.refresh()
    assert len(setup.calls) == 1
    assert setup.auth.store.read().refresh_token == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://wrong.example/"},
        {"aud": "wrong"},
        {"sub": "wrong"},
        {"azp": "wrong"},
        {"scope": "read:context"},
        {"exp": 1},
    ],
)
def test_token_pins_are_verified(setup, changes):
    with pytest.raises((ValueError, jwt.PyJWTError)):
        verify_access(setup.profile, setup.token(**changes), setup.client)


def test_login_discovers_before_installation_and_does_not_submit(setup, monkeypatch):
    monkeypatch.setattr(auth_module, "callback_code", lambda *_: ("code", "verifier"))
    setup.auth.logout()
    setup.auth.login(lambda _: pytest.fail("URL presenter"))
    assert setup.auth.status() == "active"
    bodies = [json.loads(r.content) for r in setup.calls if r.url.path == "/mcp"]
    assert [b["method"] for b in bodies] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert bodies[-1]["params"]["arguments"]["request_mode"] == "discover"


def test_logout_during_login_prevents_resurrection(setup, monkeypatch):
    def callback(*_):
        setup.auth.logout()
        return "code", "verifier"

    monkeypatch.setattr(auth_module, "callback_code", callback)
    with pytest.raises(ValueError, match="changed during login"):
        setup.auth.login(lambda _: None)
    assert setup.auth.status() == "logged_out"


def test_durable_replacement_failure_keeps_renewal_fenced(setup, monkeypatch):
    original = setup.auth.store.write

    def fail(record):
        if record.state == "active":
            original(record)
            raise OSError("final durability failure")
        original(record)

    monkeypatch.setattr(setup.auth.store, "write", fail)
    with pytest.raises(ValueError):
        setup.auth.refresh()
    assert setup.auth.status() == "reauthentication_required"
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)
    assert len([r for r in setup.calls if r.url.path == "/oauth/token"]) == 1


def test_persistent_storage_failure_after_exchange_keeps_marker(setup, monkeypatch):
    original = setup.auth.store.write

    def fail(record):
        if setup.calls:
            raise OSError("storage unavailable")
        original(record)

    monkeypatch.setattr(setup.auth.store, "write", fail)
    with pytest.raises(OSError):
        setup.auth.refresh()
    assert setup.auth.store.incomplete() is True
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)


def test_profile_binding_and_private_files(setup):
    assert "original-refresh" not in repr(setup.auth.store.read())
    changed = setup.profile.model_copy(update={"client_id": "other-client"})
    with pytest.raises(ValueError):
        profile_credentials(changed)
    setup.auth.store.path.chmod(0o644)
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)


@pytest.mark.parametrize("action", ["login", "refresh", "status", "logout"])
def test_cli_profile_dispatch_never_uses_global_auth(setup, monkeypatch, action):
    calls = []

    def run(mode, path, present):
        calls.append((mode, path))
        return "active"

    monkeypatch.setattr(auth_module, "run_reference_auth", run)
    result = CliRunner().invoke(app, ["auth", action, "--reference-profile", str(setup.path)])
    assert result.exit_code == 0, result.output
    assert calls == [(action, setup.path)]


def test_cli_errors_omit_provider_and_credential_data(setup, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "load_reference_profile",
        lambda _: (_ for _ in ()).throw(ValueError("synthetic-secret")),
    )
    result = CliRunner().invoke(app, ["auth", "refresh", "--reference-profile", str(setup.path)])
    assert result.exit_code == 1
    assert "synthetic-secret" not in result.output


CHILD = """
import sys, time, json
from pathlib import Path
import httpx
from nauro.sync import reference_auth as module
from nauro.sync.decision_profile import load_reference_profile
profile = load_reference_profile(Path(sys.argv[1]))
auth = module.ReferenceAuth(profile, httpx.Client())
log = Path(sys.argv[2])
mode = sys.argv[3]
def exchange(profile, client, grant):
    with log.open('a') as f:
        f.write(grant['refresh_token'] + '\\n')
        f.flush()
    if mode == 'pause':
        time.sleep(30)
    else:
        time.sleep(0.3)
    return ('next-access', grant['refresh_token'] + '-next', int(time.time()) + 600)
module.exchange = exchange
auth.refresh()
"""


def wait_log(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return
        time.sleep(0.02)
    pytest.fail("Child did not reach exchange")


def test_two_processes_never_exchange_the_same_rotating_token(setup, tmp_path):
    log = tmp_path / "exchanges"
    first = subprocess.Popen([sys.executable, "-c", CHILD, str(setup.path), str(log), "run"])
    try:
        wait_log(log)
        second = subprocess.run(
            [sys.executable, "-c", CHILD, str(setup.path), str(log), "run"],
            timeout=10,
            capture_output=True,
        )
        assert second.returncode == 0, second.stderr.decode()
        assert first.wait(timeout=10) == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    assert log.read_text().splitlines() == ["original-refresh", "original-refresh-next"]


def test_process_death_after_exchange_admission_requires_login(setup, tmp_path):
    log = tmp_path / "exchanges"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(setup.path), str(log), "pause"])
    try:
        wait_log(log)
    finally:
        child.kill()
        child.wait()
    assert setup.auth.status() == "reauthentication_required"
    with pytest.raises(ValueError):
        setup.auth.refresh()
    assert log.read_text().splitlines() == ["original-refresh"]


def test_callback_rejects_wrong_state_before_accepting_own_attempt(setup):
    import socket
    import threading

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = setup.profile.model_copy(update={"redirect_uri": f"http://127.0.0.1:{port}/callback"})
    responses = []
    workers = []

    def present(url):
        params = parse_qs(urlsplit(url).query)
        assert params["code_challenge_method"] == ["S256"]

        def request():
            with httpx.Client(trust_env=False) as client:
                responses.append(
                    client.get(
                        profile.redirect_uri, params={"state": "wrong", "code": "bad"}
                    ).status_code
                )
                responses.append(
                    client.get(
                        profile.redirect_uri, params={"state": params["state"][0], "code": "valid"}
                    ).status_code
                )

        thread = threading.Thread(target=request)
        workers.append(thread)
        thread.start()

    code, verifier = callback_code(profile, present, timeout=3)
    for worker in workers:
        worker.join(timeout=5)
    assert code == "valid"
    assert 43 <= len(verifier) <= 128
    assert responses == [400, 200]


@pytest.mark.parametrize("body", [[], {"token_type": 123}, {"keys": ["wrong"]}])
def test_malformed_provider_responses_are_safe(setup, body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        auth = ReferenceAuth(setup.profile, client)
        with pytest.raises(ValueError):
            auth.refresh()
        assert auth.status() == "reauthentication_required"


def test_wrong_signature_is_rejected(setup):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {"exp": int(time.time()) + 600}, other, algorithm="RS256", headers={"kid": "test-key"}
    )
    with pytest.raises(jwt.InvalidSignatureError):
        verify_access(setup.profile, token, setup.client)


@pytest.mark.parametrize("mode", ["before", "after"])
def test_process_death_around_exchange_keeps_recovery_closed(setup, tmp_path, mode):
    log = tmp_path / "checkpoint"
    bootstrap = CHILD.replace(
        "auth.refresh()",
        """
original = auth.store.write
def write(record):
    original(record)
    stop_before = mode == 'before' and record.state == 'renewal_in_progress'
    stop_after = mode == 'after' and record.state == 'active'
    if stop_before or stop_after:
        log.with_suffix('.ready').write_text('ready')
        time.sleep(30)
auth.store.write = write
auth.refresh()
""",
    )
    child = subprocess.Popen([sys.executable, "-c", bootstrap, str(setup.path), str(log), mode])
    try:
        wait_log(log.with_suffix(".ready"))
    finally:
        child.kill()
        child.wait()
    assert setup.auth.status() == "reauthentication_required"
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)
    with pytest.raises(ValueError):
        setup.auth.refresh()
    assert log.exists() is (mode == "after")
    if mode == "after":
        assert log.read_text().splitlines() == ["original-refresh"]


def test_refresh_lock_blocks_readers_and_logout_until_release(setup, tmp_path):
    log = tmp_path / "exchange"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(setup.path), str(log), "pause"])
    try:
        wait_log(log)
        with pytest.raises(ValueError, match="busy"), setup.auth.store.locked(timeout=0.05):
            pytest.fail("Entered held lock")
    finally:
        child.kill()
        child.wait()
    setup.auth.logout()
    assert setup.auth.status() == "logged_out"
    assert setup.auth.store.read().access_token == ""
    assert setup.auth.store.read().refresh_token == ""


def test_login_admission_failure_keeps_existing_credentials(setup, monkeypatch):
    monkeypatch.setattr(auth_module, "callback_code", lambda *_: ("code", "verifier"))
    before = setup.auth.store.read()
    original = setup.client._transport.handler

    def wire(request):
        if request.url.path == "/mcp":
            return httpx.Response(403)
        return original(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(ValueError):
            ReferenceAuth(setup.profile, client).login(lambda _: None)
    assert setup.auth.store.read() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"issuer": "http://issuer.example/"},
        {"issuer": "https://issuer.example/other/"},
        {"endpoint": "https://probe.example/other"},
        {"redirect_uri": "https://other.example/callback"},
        {"redirect_uri": "http://127.0.0.1:18765/callback?extra=1"},
    ],
)
def test_profile_rejects_unsafe_endpoints(setup, changes):
    with pytest.raises(ValueError):
        RenewalProfile.model_validate({**setup.profile.model_dump(), **changes})


def test_local_status_never_renews_expired_credentials(setup):
    record = setup.auth.store.read().model_copy(update={"expires_at": 1})
    with setup.auth.store.locked():
        setup.auth.store.write(record)
    assert setup.auth.status() == "expired"
    assert setup.calls == []


def test_live_stdio_subprocess_uses_renewed_profile(setup):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    bootstrap = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
import httpx
from nauro.cli.main import app
from nauro.mcp import decision_reference_startup as startup
from nauro.sync.decision_profile import load_reference_profile
from nauro.sync.decision_reference_contract import reference_schema
profile = load_reference_profile(Path(sys.argv[-1]))
log = Path(profile.credentials_file).with_suffix('.requests')
def wire(request):
    body = json.loads(request.content)
    if body['method'] == 'notifications/initialized':
        return httpx.Response(202)
    if body['method'] == 'initialize':
        result = {'protocolVersion': '2025-06-18'}
    elif body['method'] == 'tools/list':
        result = {'tools': [{'name': 'propose_decision', 'inputSchema': reference_schema()}]}
    else:
        assert body['params']['arguments']['request_mode'] == 'discover'
        import hashlib
        with log.open('a') as stream:
            digest = hashlib.sha256(request.headers['authorization'].encode()).hexdigest()
            stream.write(digest + '\\n')
        result = {'content': [{'type': 'text', 'text': json.dumps(
            {'version': 1, 'requests': [], 'next_after': None})}]}
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})
startup.httpx = SimpleNamespace(Client=lambda: httpx.Client(transport=httpx.MockTransport(wire)))
app()
"""

    async def session():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, "serve", "--reference-profile", str(setup.path)],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                first = await client.call_tool(
                    "propose_decision", {"project_id": PROJECT, "request_mode": "discover"}
                )
                setup.auth.refresh()
                second = await client.call_tool(
                    "propose_decision", {"project_id": PROJECT, "request_mode": "discover"}
                )
                assert first.isError is False
                assert second.isError is False
                assert first.content == second.content

    asyncio.run(asyncio.wait_for(session(), timeout=15))
    hashes = setup.auth.store.path.with_suffix(".requests").read_text().splitlines()
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]


@pytest.mark.parametrize("action", ["refresh", "login"])
def test_failed_marker_completion_requires_reauthentication(setup, monkeypatch, action):
    monkeypatch.setattr(auth_module, "callback_code", lambda *_: ("code", "verifier"))

    def fail_finish():
        setup.auth.store.marker.unlink()
        raise OSError("directory barrier failed")

    monkeypatch.setattr(setup.auth.store, "finish", fail_finish)
    with pytest.raises((OSError, ValueError)):
        if action == "refresh":
            setup.auth.refresh()
        else:
            setup.auth.login(lambda _: None)
    assert setup.auth.store.incomplete() is True
    with pytest.raises(ValueError):
        profile_credentials(setup.profile)


def test_failed_pre_exchange_barrier_prevents_network(setup, monkeypatch):
    calls = []
    original = setup.auth.store.sync_directory

    def barrier():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("directory barrier failed")
        original()

    monkeypatch.setattr(setup.auth.store, "sync_directory", barrier)
    with pytest.raises(OSError):
        setup.auth.refresh()
    assert setup.calls == []
    assert setup.auth.store.incomplete() is True
