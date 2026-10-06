from __future__ import annotations

import errno
import json
import socket
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.config import load_config, save_config
from nauro.store.generation_authority import GenerationAuthorityMarker
from nauro.store.registry import register_project_v2
from nauro.sync import generation_credentials as module
from nauro.sync.auth_errors import AuthenticationError
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.decision_reference_contract import reference_schema

ACTOR = "01K33333333333333333333333"
OTHER = "01K44444444444444444444444"


@pytest.fixture
def account(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("NAURO_HOME", str(home))
    for key in (
        "NAURO_AUTH0_DOMAIN",
        "NAURO_AUTH0_CLIENT_ID",
        "NAURO_API_URL",
        "NAURO_AUTH0_AUDIENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    project, store = register_project_v2(
        "Generation", [repo], mode="cloud", server_url="https://api.example"
    )
    (store / ".replica").mkdir(parents=True)
    marker = store / ".replica" / "authority.json"
    marker.write_bytes(
        GenerationAuthorityMarker(
            schema_version=1, authority="generation", project_id=project, store_format_version=1
        ).canonical_bytes()
    )
    save_config(
        {
            "auth0_domain": "issuer.example",
            "auth0_client_id": "client",
            "api_url": "https://api.example",
            "auth0_audience": "https://api.example/mcp",
            "auth": {
                "access_token": "legacy-token",
                "refresh_token": "legacy-refresh",
                "user_id": ACTOR,
            },
        }
    )
    config_before = load_config()
    connection = module.GenerationConnection(
        endpoint="https://api.example/mcp",
        issuer="https://issuer.example/",
        client_id="client",
        audience="https://api.example/mcp",
        redirect_uri="http://localhost:18457/callback",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "key"
    calls = []
    control = {"changes": {}, "me": ACTOR, "status": 200, "rpc_status": 200, "count": 0}

    def wire(request):
        calls.append(request)
        if request.url.path == "/oauth/token":
            if control.get("connect_failure"):
                raise control["connect_failure"]("synthetic secret", request=request)
            control["count"] += 1
            if control["status"] == "lost":
                raise httpx.ReadError("secret provider detail")
            claims = {
                "iss": connection.issuer,
                "aud": connection.audience,
                "azp": connection.client_id,
                "sub": "owner",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "scope": "read:context write:context",
                "jti": str(control["count"]),
            }
            claims.update(control["changes"])
            token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "key"})
            body = {
                "access_token": token,
                "refresh_token": f"rotated-{control['count']}",
                "token_type": "Bearer",
            }
            if control.get("omit_refresh"):
                del body["refresh_token"]
            return httpx.Response(control["status"], json=body)
        if request.url.path == "/.well-known/jwks.json":
            if control.get("jwks_failure"):
                raise httpx.ConnectError("synthetic secret", request=request)
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": control["me"]})
        assert request.url == connection.endpoint
        body = json.loads(request.content)
        if control["rpc_status"] != 200:
            return httpx.Response(control["rpc_status"])
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

    original_client = httpx.Client

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(wire), **kwargs)

    monkeypatch.setattr("nauro.cli.generation_auth.httpx.Client", client)
    monkeypatch.setattr(module, "callback_code", lambda *_: ("code", "verifier"))
    yield SimpleNamespace(
        connection=connection,
        project=project,
        store=store,
        marker=marker,
        calls=calls,
        control=control,
        client=client,
        config_before=config_before,
    )


def command(action):
    return CliRunner().invoke(app, ["auth", action])


def test_non_rotating_renewal_keeps_refresh_token_and_verifies_access(account):
    assert command("login").exit_code == 0
    before = account.connection.store().read()
    account.control["omit_refresh"] = True
    account.calls.clear()
    assert command("refresh").exit_code == 0
    after = account.connection.store().read()
    assert after.refresh_token == before.refresh_token
    assert after.revision != before.revision
    assert after.subject == before.subject
    assert account.connection.store().incomplete() is False
    assert [r.url.path for r in account.calls] == ["/oauth/token", "/.well-known/jwks.json"]
    assert command("status").stdout.strip() == "active"


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_unsent_renewal_preserves_credentials_for_explicit_retry(account, failure):
    assert command("login").exit_code == 0
    before = account.connection.store().read()
    account.control["connect_failure"] = failure
    account.calls.clear()
    result = command("refresh")
    assert result.exit_code == 1
    assert "Could not connect to the token endpoint" in result.output
    assert "synthetic secret" not in result.output
    assert account.connection.store().read() == before
    assert account.connection.store().incomplete() is False
    assert len(account.calls) == 1
    del account.control["connect_failure"]
    assert command("refresh").exit_code == 0


@pytest.mark.parametrize("non_rotating", [False, True])
def test_signing_key_failure_can_resume_without_another_exchange(account, non_rotating):
    assert command("login").exit_code == 0
    before = account.connection.store().read()
    account.control["omit_refresh"] = non_rotating
    account.calls.clear()
    account.control["jwks_failure"] = True
    result = command("refresh")
    assert result.exit_code == 1
    assert "retry verification without another token exchange" in result.output
    assert "synthetic secret" not in result.output
    pending = account.connection.store().read()
    assert pending.refresh_token == (before.refresh_token if non_rotating else "rotated-2")
    assert pending.access_token != before.access_token
    assert account.connection.store().incomplete() is True
    assert command("status").stdout.strip() == "verification_required"
    with pytest.raises(ValueError):
        module.generation_credentials(account.connection, ACTOR)
    account.control["jwks_failure"] = False
    assert command("refresh").exit_code == 0
    after = account.connection.store().read()
    assert after.access_token == pending.access_token
    assert after.refresh_token == pending.refresh_token
    assert after.subject == before.subject
    assert after.user_id == before.user_id
    assert command("status").stdout.strip() == "active"
    assert [r.url.path for r in account.calls] == [
        "/oauth/token",
        "/.well-known/jwks.json",
        "/.well-known/jwks.json",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example/"},
        {"aud": "other"},
        {"azp": "other"},
        {"sub": "other"},
        {"scope": "read:context"},
    ],
)
def test_retry_never_admits_invalid_received_credentials(account, changes):
    assert command("login").exit_code == 0
    account.calls.clear()
    account.control["changes"] = changes
    assert command("refresh").exit_code == 1
    pending = account.connection.store().read()
    account.control["changes"] = {}
    assert command("refresh").exit_code == 1
    assert account.connection.store().read() == pending
    assert command("status").stdout.strip() == "verification_required"
    with pytest.raises(ValueError):
        module.generation_credentials(account.connection, ACTOR)
    assert [r.url.path for r in account.calls] == [
        "/oauth/token",
        "/.well-known/jwks.json",
        "/.well-known/jwks.json",
    ]


def test_logout_discards_pending_verification_and_prevents_recovery(account):
    assert command("login").exit_code == 0
    account.control["jwks_failure"] = True
    assert command("refresh").exit_code == 1
    account.calls.clear()
    assert command("logout").exit_code == 0
    account.control["jwks_failure"] = False
    assert command("refresh").exit_code == 1
    assert command("status").stdout.strip() == "logged_out"
    record = account.connection.store().read()
    assert record.access_token == ""
    assert record.refresh_token == ""
    assert account.calls == []


def test_clock_recovery_verifies_the_saved_response_without_another_exchange(account, monkeypatch):
    from datetime import datetime, timezone

    assert command("login").exit_code == 0
    clock = {"now": int(time.time())}

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock["now"], timezone.utc)

    monkeypatch.setattr("jwt.api_jwt.datetime", Clock)
    monkeypatch.setattr("nauro.sync.reference_oauth.time.time", lambda: clock["now"])
    account.control["changes"] = {"iat": clock["now"] + 10}
    account.calls.clear()
    result = command("refresh")
    assert result.exit_code == 1
    assert "timestamp is ahead of this machine's clock" in result.output
    assert command("status").stdout.strip() == "verification_required"
    pending = account.connection.store().read()
    with pytest.raises(ValueError):
        module.generation_credentials(account.connection, ACTOR)
    clock["now"] += 6
    assert command("refresh").exit_code == 0
    assert account.connection.store().read().access_token == pending.access_token
    assert account.connection.store().read().refresh_token == pending.refresh_token
    assert command("status").stdout.strip() == "active"
    assert [r.url.path for r in account.calls] == [
        "/oauth/token",
        "/.well-known/jwks.json",
        "/.well-known/jwks.json",
    ]


def test_expired_saved_response_stays_blocked_without_reusing_refresh_token(account):
    assert command("login").exit_code == 0
    account.control["changes"] = {"exp": 1}
    account.calls.clear()
    for _ in range(2):
        result = command("refresh")
        assert result.exit_code == 1
        assert "access token has expired" in result.output
        assert "Run 'nauro auth login'" in result.output
        assert "Run 'nauro auth refresh'" not in result.output
        with pytest.raises(ValueError):
            module.generation_credentials(account.connection, ACTOR)
    assert len([r for r in account.calls if r.url.path == "/oauth/token"]) == 1


def test_initial_login_still_requires_a_refresh_token(account):
    account.control["omit_refresh"] = True
    result = command("login")
    assert result.exit_code == 1
    assert "token response is incomplete or invalid" in result.output
    assert account.connection.store().read() is None


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (AuthenticationError("login_timeout"), "Login timed out"),
        (AuthenticationError("login_refused"), "Login was refused"),
        (PermissionError("synthetic secret"), "access was denied"),
        (OSError(errno.EADDRINUSE, "synthetic secret"), "callback port is in use"),
    ],
)
def test_login_reports_safe_actionable_errors(account, monkeypatch, failure, message):
    def callback(*args):
        raise failure

    monkeypatch.setattr(module, "callback_code", callback)
    result = command("login")
    assert result.exit_code == 1
    assert message in result.output
    assert "synthetic secret" not in result.output


def test_normal_lifecycle_preserves_legacy_credentials(account):
    assert command("login").exit_code == 0
    record = account.connection.store().read()
    assert record.subject == "owner"
    assert record.user_id == ACTOR
    assert record.version == 3
    assert load_config() == account.config_before
    assert command("status").stdout.strip() == "active"
    with account.client() as client:
        transport = DecisionReferenceTransport(
            account.connection.endpoint,
            account.project,
            ACTOR,
            client,
            lambda: module.generation_credentials(account.connection, ACTOR),
        )
        transport.initialize()
        assert command("refresh").exit_code == 0
        transport.propose_decision(project_id=account.project, request_mode="discover")
        assert (
            account.calls[-1].headers["Authorization"]
            == "Bearer " + account.connection.store().read().access_token
        )
        assert command("logout").exit_code == 0
        before = len(account.calls)
        with pytest.raises(ValueError, match="login or explicit refresh"):
            transport.propose_decision(project_id=account.project, request_mode="discover")
        assert len(account.calls) == before
    assert command("status").stdout.strip() == "logged_out"
    assert load_config() == account.config_before


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://other.example/"},
        {"aud": "other"},
        {"azp": "other"},
        {"sub": ""},
        {"scope": "read:context"},
        {"exp": 1},
    ],
)
def test_invalid_token_never_reaches_identity_endpoint(account, change):
    account.control["changes"] = change
    assert command("login").exit_code == 1
    assert [r.url.path for r in account.calls] == ["/oauth/token", "/.well-known/jwks.json"]
    assert account.connection.store().read() is None
    assert load_config() == account.config_before


@pytest.mark.parametrize("identity", [None, "", "owner", OTHER.lower()])
def test_missing_canonical_identity_installs_nothing(account, identity):
    account.control["me"] = identity
    assert command("login").exit_code == 1
    assert account.connection.store().read() is None
    assert len([r for r in account.calls if r.url.path == "/mcp"]) == 0


@pytest.mark.parametrize("problem", ["endpoint", "marker"])
def test_invalid_selection_refuses_before_authentication(account, problem):
    if problem == "endpoint":
        config = load_config()
        config["api_url"] = "https://other.example"
        save_config(config)
    else:
        account.marker.write_text("broken")
    assert command("login").exit_code == 1
    assert account.calls == []


def test_no_replica_controls_keeps_legacy_status_and_refresh_contract(account):
    account.marker.unlink()
    account.marker.parent.rmdir()
    assert "Authenticated as:" in command("status").stdout
    assert command("refresh").exit_code == 2
    assert account.calls == []
    assert account.connection.store().read() is None


@pytest.mark.parametrize("action", ["login", "status", "refresh", "logout"])
def test_incomplete_replica_refuses_auth_without_legacy_fallback(account, action):
    account.marker.unlink()
    result = command(action)
    assert result.exit_code == 1
    assert account.calls == []
    assert load_config() == account.config_before
    assert account.connection.store().read() is None
    assert account.marker.parent.is_dir()
    assert list(account.marker.parent.iterdir()) == []


@pytest.mark.parametrize("status", [401, 429, 503, "lost"])
def test_uncertain_normal_renewal_is_fenced_without_retry(account, status):
    assert command("login").exit_code == 0
    account.calls.clear()
    account.control["status"] = status
    assert command("refresh").exit_code == 1
    assert len(account.calls) == 1
    assert command("status").stdout.strip() == "reauthentication_required"
    assert command("refresh").exit_code == 1
    assert len(account.calls) == 1
    assert account.connection.store().read().state == "renewal_in_progress"


def test_expiry_status_and_restart_do_not_renew(account):
    assert command("login").exit_code == 0
    store = account.connection.store()
    with store.locked():
        record = store.read()
        record.expires_at = 1
        store.write(record)
    account.calls.clear()
    assert command("status").stdout.strip() == "expired"
    with pytest.raises(ValueError, match="login or explicit refresh"):
        module.generation_credentials(account.connection, ACTOR)
    assert account.calls == []
    assert command("refresh").exit_code == 0
    assert module.generation_credentials(account.connection, ACTOR).user_id == ACTOR
    with pytest.raises(ValueError):
        module.generation_credentials(account.connection, OTHER)


def test_changed_subject_fences_renewal(account):
    assert command("login").exit_code == 0
    account.control["changes"] = {"sub": "other"}
    assert command("refresh").exit_code == 1
    assert account.connection.store().read().state == "renewal_in_progress"


def test_logout_during_normal_login_prevents_resurrection(account, monkeypatch):
    assert command("login").exit_code == 0

    def callback(*_):
        assert command("logout").exit_code == 0
        return "code", "verifier"

    monkeypatch.setattr(module, "callback_code", callback)
    assert command("login").exit_code == 1
    assert account.connection.store().read().state == "logged_out"


def test_failed_project_admission_preserves_old_record(account):
    assert command("login").exit_code == 0
    before = account.connection.store().path.read_bytes()
    account.control["rpc_status"] = 403
    assert command("login").exit_code == 1
    assert account.connection.store().path.read_bytes() == before


def test_two_projects_share_one_normal_credential_store(account, tmp_path, monkeypatch):
    assert command("login").exit_code == 0
    second_repo = tmp_path / "second"
    second_repo.mkdir()
    project, store = register_project_v2(
        "Second", [second_repo], mode="cloud", server_url="https://api.example"
    )
    (store / ".replica").mkdir(parents=True)
    (store / ".replica" / "authority.json").write_bytes(
        GenerationAuthorityMarker(
            schema_version=1, authority="generation", project_id=project, store_format_version=1
        ).canonical_bytes()
    )
    monkeypatch.chdir(second_repo)
    before = len(account.calls)
    assert command("status").stdout.strip() == "active"
    assert len(account.calls) == before
    assert command("refresh").exit_code == 0
    assert account.connection.store().read().refresh_token == "rotated-2"
    assert (
        len(list(account.connection.store().path.parent.glob("generation-credentials-*.json"))) == 1
    )


@pytest.mark.parametrize("failure", ["marker", "completion"])
def test_normal_renewal_durability_failure_closes_admission(account, monkeypatch, failure):
    assert command("login").exit_code == 0
    account.calls.clear()

    def fail(*_):
        raise OSError("durability failure")

    monkeypatch.setattr(module.AccountStore, "begin" if failure == "marker" else "finish", fail)
    assert command("refresh").exit_code == 1
    if failure == "marker":
        assert account.calls == []
    else:
        assert account.connection.store().incomplete() is True
        assert command("status").stdout.strip() == "verification_required"
        with pytest.raises(ValueError):
            module.generation_credentials(account.connection, ACTOR)


def test_normal_credentials_do_not_refresh_or_resend_a_submission(account):
    assert command("login").exit_code == 0
    with account.client() as client:
        transport = DecisionReferenceTransport(
            account.connection.endpoint,
            account.project,
            ACTOR,
            client,
            lambda: module.generation_credentials(account.connection, ACTOR),
        )
        transport.initialize()
        account.calls.clear()
        account.control["rpc_status"] = 401
        with pytest.raises(ValueError):
            transport.propose_decision(
                project_id=account.project,
                request_mode="submit",
                operation_id="decision-request:01K55555555555555555555555",
                payload_digest="a" * 64,
            )
        assert len(account.calls) == 1
        assert (
            json.loads(account.calls[0].content)["params"]["arguments"]["request_mode"] == "submit"
        )
        assert account.connection.store().read().refresh_token == "rotated-1"
