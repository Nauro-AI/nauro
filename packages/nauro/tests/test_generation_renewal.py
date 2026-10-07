from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from nauro.sync import generation_renewal as renewal
from nauro.sync.auth_errors import AuthenticationError
from tests.test_generation_credentials import ACTOR, OTHER, account, command

__all__ = ["account"]
_SUPERVISOR = renewal._run_worker


@pytest.fixture
def automatic(account, monkeypatch):
    assert command("login").exit_code == 0
    account.calls.clear()

    def run(request, timeout):
        assert 0 < timeout <= renewal.RENEWAL_TIMEOUT_SECONDS
        with account.client() as client:
            renewal.renew_requested_credentials(request, client)

    monkeypatch.setattr(renewal, "_run_worker", run)
    return account


def expire(account, seconds=-1):
    store = account.connection.store()
    with store.locked():
        record = store.read().model_copy(update={"expires_at": int(time.time()) + seconds})
        store.write(record)
    return record


def acquire(account):
    return renewal.acquire_generation_credentials(account.connection, account.project, ACTOR)


def request_for(account):
    record = account.connection.store().read()
    return renewal.RenewalRequest(
        connection=account.connection,
        project=account.project,
        actor=record.user_id,
        subject=record.subject,
        revision=record.revision,
        deadline=time.time() + renewal.RENEWAL_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize("remaining", [-1, 30])
@pytest.mark.parametrize("omit_refresh", [False, True])
def test_expiry_renews_once_then_reuses_fresh_credentials(automatic, remaining, omit_refresh):
    before = expire(automatic, remaining)
    automatic.control["omit_refresh"] = omit_refresh
    first = acquire(automatic)
    second = acquire(automatic)
    after = automatic.connection.store().read()
    assert first == second
    assert first.access_token == after.access_token
    assert after.refresh_token == (before.refresh_token if omit_refresh else "rotated-2")
    assert after.subject == before.subject
    assert [r.url.path for r in automatic.calls] == ["/oauth/token", "/.well-known/jwks.json"]


def test_fresh_credentials_and_status_never_start_worker(automatic, monkeypatch):
    def forbidden(*args):
        pytest.fail("Unexpected renewal")

    monkeypatch.setattr(renewal, "_run_worker", forbidden)
    assert acquire(automatic).user_id == ACTOR
    expire(automatic)
    assert command("status").output.strip() == "expired"
    assert automatic.calls == []


def test_real_worker_reuses_the_winners_result(automatic):
    expire(automatic)
    request = request_for(automatic)
    expected = acquire(automatic)
    _SUPERVISOR(request, renewal.RENEWAL_TIMEOUT_SECONDS)
    assert acquire(automatic) == expected
    assert [r.url.path for r in automatic.calls] == ["/oauth/token", "/.well-known/jwks.json"]


def test_simultaneous_acquisition_uses_one_exchange(automatic):
    expire(automatic)
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda _: acquire(automatic), range(5)))
    assert outcomes == [outcomes[0]] * 5
    assert [r.url.path for r in automatic.calls] == ["/oauth/token", "/.well-known/jwks.json"]


def test_failed_exchange_is_not_replayed(automatic):
    expire(automatic)
    automatic.control["status"] = "lost"
    for _ in range(2):
        with pytest.raises(ValueError):
            acquire(automatic)
    record = automatic.connection.store().read()
    assert record.state == "renewal_in_progress"
    assert record.refresh_token == ""
    assert [r.url.path for r in automatic.calls] == ["/oauth/token"]


def test_unsent_failure_backoff_is_shared_and_explicit_refresh_can_recover(automatic):
    before = expire(automatic)
    automatic.control["connect_failure"] = httpx.ConnectError
    with pytest.raises(ValueError):
        acquire(automatic)
    assert automatic.connection.store().read() == before
    with pytest.raises(AuthenticationError, match="paused"):
        acquire(automatic)
    del automatic.control["connect_failure"]
    assert command("refresh").exit_code == 0
    assert acquire(automatic).user_id == ACTOR
    assert [r.url.path for r in automatic.calls] == [
        "/oauth/token",
        "/oauth/token",
        "/.well-known/jwks.json",
    ]


def test_pending_verification_recovers_without_exchange(automatic):
    expire(automatic)
    automatic.control["jwks_failure"] = True
    with pytest.raises(ValueError):
        acquire(automatic)
    pending = automatic.connection.store().read()
    assert pending.needs_verification() is True
    automatic.control["jwks_failure"] = False
    retry = automatic.connection.store().path.with_suffix(".renewal-retry.json")
    saved = renewal.RenewalRetry.model_validate_json(retry.read_text())
    retry.write_text(
        saved.model_copy(update={"retry_after": int(time.time()) - 1}).model_dump_json()
    )
    assert acquire(automatic).access_token == pending.access_token
    assert [r.url.path for r in automatic.calls] == [
        "/oauth/token",
        "/.well-known/jwks.json",
        "/.well-known/jwks.json",
    ]


def test_worker_failure_keeps_a_valid_credential_inside_the_window(automatic):
    before = expire(automatic, 30)
    automatic.control["connect_failure"] = httpx.ConnectError
    assert acquire(automatic).access_token == before.access_token
    assert acquire(automatic).access_token == before.access_token  # paused attempt
    assert automatic.connection.store().read() == before
    assert [r.url.path for r in automatic.calls] == ["/oauth/token"]
    expire(automatic)
    with pytest.raises(ValueError, match="paused"):
        acquire(automatic)


def test_acquisition_waits_for_a_peer_renewal_and_reuses_its_result(automatic, monkeypatch):
    expire(automatic)
    store = automatic.connection.store()
    monkeypatch.setattr(renewal, "_run_worker", lambda *_: pytest.fail("Unexpected renewal"))
    fresh = store.read().model_copy(
        update={
            "revision": "peer",
            "access_token": "peer-token",
            "expires_at": int(time.time()) + 600,
        }
    )

    def peer():
        with store.locked():
            time.sleep(2.5)
            store.write(fresh)

    thread = threading.Thread(target=peer)
    thread.start()
    time.sleep(0.3)
    try:
        assert acquire(automatic).access_token == "peer-token"
    finally:
        thread.join()
    assert automatic.calls == []


def test_invalid_retry_record_is_replaced(automatic):
    expire(automatic)
    store = automatic.connection.store()
    path = store.path.with_suffix(".renewal-retry.json")
    store.write_private(path, b"not json")
    assert acquire(automatic).user_id == ACTOR
    assert renewal.RenewalRetry.model_validate_json(path.read_text()).actor == ACTOR
    assert [r.url.path for r in automatic.calls] == ["/oauth/token", "/.well-known/jwks.json"]


@pytest.mark.parametrize("alias", [False, True])
def test_worker_ignores_modules_in_the_callers_directory(automatic, monkeypatch, tmp_path, alias):
    expire(automatic)
    request = request_for(automatic)
    acquire(automatic)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    entry = shadow
    if alias:
        # The import path names the working directory through a symlink alias.
        entry = tmp_path / "alias"
        try:
            entry.symlink_to(shadow, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    for name in ("secrets", "jwt", "httpx"):
        (shadow / f"{name}.py").write_text("raise SystemExit('shadowed import executed')\n")
    marker = tmp_path / "site-hook-ran"
    for hook in ("sitecustomize", "usercustomize"):
        (shadow / f"{hook}.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    monkeypatch.chdir(shadow)
    monkeypatch.syspath_prepend(str(entry))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([".", os.environ.get("PYTHONPATH", "")]))
    _SUPERVISOR(request, renewal.RENEWAL_TIMEOUT_SECONDS)
    assert [r.url.path for r in automatic.calls] == ["/oauth/token", "/.well-known/jwks.json"]
    assert not marker.exists()


def test_stalled_response_after_send_requires_login(automatic):
    # A request that reached the endpoint is an uncertain exchange: the deadline bounds
    # the wait, but the saved credentials are not restored, even with validity left.
    expire(automatic, 30)
    automatic.control["connect_failure"] = httpx.ReadTimeout
    with pytest.raises(ValueError, match="login required"):
        acquire(automatic)
    store = automatic.connection.store()
    assert store.incomplete() is True
    assert store.read().refresh_token == ""
    (request,) = automatic.calls
    seen = request.extensions["timeout"]
    limit = renewal.RENEWAL_TIMEOUT_SECONDS - renewal.EXCHANGE_MARGIN_SECONDS
    assert seen["connect"] + seen["read"] <= limit
    del automatic.control["connect_failure"]
    assert command("refresh").exit_code != 0
    with pytest.raises(AuthenticationError, match="(?i)login"):
        acquire(automatic)
    assert [r.url.path for r in automatic.calls] == ["/oauth/token"]


def test_exchange_timeout_is_bounded_by_the_request_deadline(automatic):
    before = expire(automatic)
    automatic.control["connect_failure"] = httpx.ConnectTimeout
    with pytest.raises(ValueError, match="Could not connect"):
        acquire(automatic)
    store = automatic.connection.store()
    assert store.read() == before
    assert store.incomplete() is False
    (request,) = automatic.calls
    seen = request.extensions["timeout"]
    limit = renewal.RENEWAL_TIMEOUT_SECONDS - renewal.EXCHANGE_MARGIN_SECONDS
    assert 0 < seen["connect"] <= seen["read"]
    assert seen["connect"] + seen["read"] <= limit


def test_worker_refuses_to_start_an_exchange_without_time_left(automatic):
    before = expire(automatic)
    request = request_for(automatic).model_copy(update={"deadline": time.time() + 1.0})
    with automatic.client() as client, pytest.raises(ValueError, match="timed out"):
        renewal.renew_requested_credentials(request, client)
    assert automatic.connection.store().read() == before
    assert automatic.calls == []


@pytest.mark.parametrize("change", ["logout", "actor", "subject"])
def test_selected_account_change_prevents_renewal(automatic, change):
    expire(automatic)
    request = request_for(automatic)
    store = automatic.connection.store()
    with store.locked():
        before = store.read()
        after = (
            store.empty("logged_out")
            if change == "logout"
            else before.model_copy(
                update={"user_id": OTHER} if change == "actor" else {"subject": "other"}
            )
        )
        store.write(after)
    with automatic.client() as client, pytest.raises(AuthenticationError):
        renewal.renew_requested_credentials(request, client)
    assert automatic.calls == []
    assert store.read() == after


WORKER = """
import json, sys, time
from pathlib import Path
import httpx, jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from nauro.sync import generation_renewal as module
from nauro.sync.reference_credentials import CredentialStore
request = module.RenewalRequest.model_validate_json(sys.stdin.read())
log = Path(sys.argv[1])
mode = sys.argv[2]
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
jwk['kid'] = 'synthetic'
def wire(http_request):
    if http_request.url.path == '/oauth/token':
        with log.open('a') as stream:
            stream.write('exchange\\n')
        if mode == 'exchange_pause':
            time.sleep(30)
        token = jwt.encode({
            'iss': request.connection.issuer, 'aud': request.connection.audience,
            'azp': request.connection.client_id, 'sub': request.subject,
            'iat': int(time.time()), 'exp': int(time.time()) + 600,
            'scope': 'read:context write:context',
        }, key, algorithm='RS256', headers={'kid': 'synthetic'})
        return httpx.Response(200, json={
            'access_token': token, 'refresh_token': 'next-synthetic', 'token_type': 'Bearer'
        })
    if mode == 'verification_pause':
        log.with_suffix('.saved').write_text('ready')
        time.sleep(30)
    return httpx.Response(200, json={'keys': [jwk]})
with httpx.Client(transport=httpx.MockTransport(wire)) as client:
    module.renew_requested_credentials(request, client)
sys.stdout.write(module.RenewalOutcome().model_dump_json())
"""


def test_processes_recheck_freshness_under_the_same_lock(automatic, tmp_path):
    expire(automatic)
    request = request_for(automatic)
    log = tmp_path / "exchanges"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(log), "normal"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    try:
        for process in processes:
            process.stdin.write(request.model_dump_json())
            process.stdin.close()
        for process in processes:
            assert process.wait(timeout=10) == 0, process.stderr.read()
            assert json.loads(process.stdout.read()) == {"error": None}
        assert log.read_text().splitlines() == ["exchange"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


@pytest.mark.parametrize("mode,pending", [("exchange_pause", False), ("verification_pause", True)])
def test_deadline_kills_worker_and_preserves_truthful_recovery(
    automatic, tmp_path, monkeypatch, mode, pending
):
    expire(automatic)
    request = request_for(automatic)
    log = tmp_path / "exchanges"
    run = subprocess.run
    child = []

    def start(args, **kwargs):
        child.append(args)
        return run([sys.executable, "-c", WORKER, str(log), mode], **kwargs)

    monkeypatch.setattr(renewal.subprocess, "run", start)
    started = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        _SUPERVISOR(request, renewal.RENEWAL_TIMEOUT_SECONDS)
    assert time.monotonic() - started < renewal.RENEWAL_TIMEOUT_SECONDS + 5
    assert len(child) == 1
    assert log.read_text().splitlines() == ["exchange"]
    store = automatic.connection.store()
    with store.locked():
        record = store.read()
        assert store.incomplete() is True
        assert record.needs_verification() is pending
        assert record.refresh_token == ("next-synthetic" if pending else "")
