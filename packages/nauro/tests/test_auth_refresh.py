"""Tests for nauro auth refresh helpers — refresh_access_token + with_token_refresh.

The refresh path is exercised on every 401 from the new sync endpoints, so
the contract here is load-bearing for the entire Tier 2 cutover. The two
behaviors that get repeated through the suite:

* On a 200 from Auth0, the access token (and rotated refresh token, if
  present) are persisted before returning.
* On any failure, stored tokens are left intact so the user can retry
  ``nauro auth login`` without losing state.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

import nauro.cli.commands.auth as auth_mod
from nauro.cli.commands.auth import (
    AuthRefreshError,
    refresh_access_token,
    with_token_refresh,
)
from nauro.store.config import load_config, save_config
from tests.conftest import seed_auth_config


def _seed_auth(refresh_token: str = "refresh_orig", access_token: str = "access_orig") -> None:
    seed_auth_config(variant="sync", access_token=access_token, refresh_token=refresh_token)


def _mock_post(status_code: int = 200, payload: dict | None = None, headers: dict | None = None):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.text = str(payload or "")
    # spec=httpx.Response does not expose ``headers`` as a mock attribute, so
    # the 429 backoff path needs a real mapping to read Retry-After from.
    response.headers = headers or {}
    return response


# --- refresh_access_token ---


class TestRefreshAccessToken:
    def test_refresh_success_persists_new_access_token(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        with patch(
            "nauro.cli.commands.auth.httpx.post",
            return_value=_mock_post(200, {"access_token": "access_new"}),
        ):
            new_token = refresh_access_token()

        assert new_token == "access_new"
        auth = load_config()["auth"]
        assert auth["access_token"] == "access_new"
        # Refresh token preserved when the server doesn't rotate it
        assert auth["refresh_token"] == "refresh_orig"

    def test_refresh_rotates_refresh_token_when_returned(self):
        _seed_auth(refresh_token="refresh_orig")

        with patch(
            "nauro.cli.commands.auth.httpx.post",
            return_value=_mock_post(
                200, {"access_token": "access_new", "refresh_token": "refresh_rotated"}
            ),
        ):
            refresh_access_token()

        auth = load_config()["auth"]
        assert auth["access_token"] == "access_new"
        assert auth["refresh_token"] == "refresh_rotated"

    def test_refresh_preserves_old_refresh_token_when_absent_in_response(self):
        _seed_auth(refresh_token="refresh_orig")

        with patch(
            "nauro.cli.commands.auth.httpx.post",
            return_value=_mock_post(200, {"access_token": "access_new"}),
        ):
            refresh_access_token()

        auth = load_config()["auth"]
        assert auth["refresh_token"] == "refresh_orig"

    def test_refresh_failure_invalid_grant_leaves_tokens_intact(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        bad = _mock_post(
            400,
            {"error": "invalid_grant", "error_description": "refresh token expired"},
        )
        with (
            patch("nauro.cli.commands.auth.httpx.post", return_value=bad),
            pytest.raises(AuthRefreshError),
        ):
            refresh_access_token()

        auth = load_config()["auth"]
        # Both tokens must survive a failed refresh — the user might retry.
        assert auth["access_token"] == "access_orig"
        assert auth["refresh_token"] == "refresh_orig"

    def test_refresh_network_error_leaves_tokens_intact(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        with (
            patch(
                "nauro.cli.commands.auth.httpx.post",
                side_effect=httpx.ConnectError("dns failure"),
            ),
            pytest.raises(AuthRefreshError),
        ):
            refresh_access_token()

        auth = load_config()["auth"]
        assert auth["access_token"] == "access_orig"
        assert auth["refresh_token"] == "refresh_orig"

    def test_refresh_without_stored_refresh_token_raises(self):
        save_config({"auth": {"sub": "auth0|test", "access_token": "access_orig"}})

        with pytest.raises(AuthRefreshError, match="No refresh token"):
            refresh_access_token()


# --- refresh_access_token 429 backoff ---


class TestRefreshRateLimitBackoff:
    def test_429_then_200_retries_and_succeeds(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        rate_limited = _mock_post(429, {"error": "too_many_requests"})
        ok = _mock_post(200, {"access_token": "access_new"})
        post = MagicMock(side_effect=[rate_limited, ok])

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep") as sleep,
        ):
            new_token = refresh_access_token()

        assert new_token == "access_new"
        assert load_config()["auth"]["access_token"] == "access_new"
        assert post.call_count == 2
        assert sleep.call_count == 1

    def test_429_exhausted_raises_and_preserves_tokens(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        rate_limited = _mock_post(429, {"error": "too_many_requests"})
        post = MagicMock(return_value=rate_limited)

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep"),
            pytest.raises(AuthRefreshError, match="rate-limiting"),
        ):
            refresh_access_token()

        assert post.call_count == 3
        auth = load_config()["auth"]
        assert auth["access_token"] == "access_orig"
        assert auth["refresh_token"] == "refresh_orig"

    def test_non_429_failure_does_not_retry_or_sleep(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        bad = _mock_post(
            400,
            {"error": "invalid_grant", "error_description": "refresh token expired"},
        )
        post = MagicMock(return_value=bad)

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep") as sleep,
            pytest.raises(AuthRefreshError),
        ):
            refresh_access_token()

        assert post.call_count == 1
        sleep.assert_not_called()

    def test_retry_after_header_honored(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        rate_limited = _mock_post(429, {"error": "too_many_requests"}, headers={"Retry-After": "2"})
        ok = _mock_post(200, {"access_token": "access_new"})
        post = MagicMock(side_effect=[rate_limited, ok])

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep") as sleep,
        ):
            refresh_access_token()

        sleep.assert_called_once_with(2.0)

    def test_retry_after_unicode_digit_falls_back_without_raising(self):
        # "²" is a Unicode digit that str.isdigit accepts but int rejects;
        # the header parse must fall back to the default backoff, not crash.
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        rate_limited = _mock_post(429, {"error": "too_many_requests"}, headers={"Retry-After": "²"})
        ok = _mock_post(200, {"access_token": "access_new"})
        post = MagicMock(side_effect=[rate_limited, ok])

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep") as sleep,
        ):
            new_token = refresh_access_token()

        assert new_token == "access_new"
        assert post.call_count == 2
        sleep.assert_called_once_with(1.0)


# --- refresh_access_token single-flight ---


class TestRefreshSingleFlight:
    def test_barrier_race_collapses_to_one_exchange(self):
        # N threads released together all present the same stale token. The
        # in-process lock elects one exchange; the rest take the fast path on
        # the freshly stored access token. Exactly one POST, all get the token.
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        n = 8
        barrier = threading.Barrier(n)
        results: list[str] = []
        errors: list[BaseException] = []
        collect = threading.Lock()

        post = MagicMock(return_value=_mock_post(200, {"access_token": "access_new"}))

        def worker() -> None:
            barrier.wait()
            try:
                token = refresh_access_token(stale_access_token="access_orig")
            except BaseException as exc:  # noqa: BLE001 - re-raised via assert below
                with collect:
                    errors.append(exc)
                return
            with collect:
                results.append(token)

        with patch("nauro.cli.commands.auth.httpx.post", post):
            threads = [threading.Thread(target=worker) for _ in range(n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert errors == []
        assert post.call_count == 1
        assert results == ["access_new"] * n
        assert load_config()["auth"]["access_token"] == "access_new"

    def test_loser_fast_path_returns_stored_token_without_network(self):
        # The stored access token already differs from the stale one, so a
        # concurrent refresher must have committed first: return it, no network.
        _seed_auth(refresh_token="refresh_orig", access_token="access_fresh")

        post = MagicMock()
        with patch("nauro.cli.commands.auth.httpx.post", post):
            token = refresh_access_token(stale_access_token="access_stale")

        assert token == "access_fresh"
        post.assert_not_called()

    def test_lock_timeout_raises_without_unguarded_exchange(self):
        # With the in-process lock already held, a bounded acquire must fail
        # loudly rather than hang or exchange unguarded.
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        post = MagicMock()
        auth_mod._REFRESH_LOCK.acquire()
        try:
            with (
                patch("nauro.cli.commands.auth.httpx.post", post),
                patch.object(auth_mod, "_REFRESH_LOCK_TIMEOUT_SECONDS", 0.05),
                pytest.raises(AuthRefreshError),
            ):
                refresh_access_token(stale_access_token="access_orig")
        finally:
            auth_mod._REFRESH_LOCK.release()

        post.assert_not_called()

    def test_commit_defers_when_refresh_token_changed_mid_exchange(self):
        # A concurrent login rotates the stored tokens while our exchange is in
        # flight. The commit re-validate must defer to that state, not clobber
        # it with the token we minted against the now-superseded refresh token.
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        def concurrent_then_ok(*_args, **_kwargs):
            save_config(
                {
                    "auth": {
                        "sub": "auth0|test",
                        "access_token": "access_other",
                        "refresh_token": "refresh_other",
                    }
                }
            )
            return _mock_post(200, {"access_token": "access_new"})

        with patch("nauro.cli.commands.auth.httpx.post", side_effect=concurrent_then_ok):
            token = refresh_access_token(stale_access_token="access_orig")

        assert token == "access_other"
        auth = load_config()["auth"]
        assert auth["access_token"] == "access_other"
        assert auth["refresh_token"] == "refresh_other"

    def test_commit_raises_when_auth_cleared_mid_exchange(self):
        # A concurrent logout clears the auth section while our exchange is in
        # flight. The commit must fail loudly rather than resurrect the token we
        # minted, and it must leave the store logged out.
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        def logout_then_ok(*_args, **_kwargs):
            save_config({})
            return _mock_post(200, {"access_token": "access_new"})

        with (
            patch("nauro.cli.commands.auth.httpx.post", side_effect=logout_then_ok),
            pytest.raises(AuthRefreshError, match="cleared during refresh"),
        ):
            refresh_access_token(stale_access_token="access_orig")

        # The minted token was not written back; the store stays logged out.
        assert "auth" not in load_config()


# --- with_token_refresh ---


class TestWithTokenRefresh:
    def test_first_call_succeeds_no_refresh(self):
        _seed_auth(access_token="access_orig")

        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        call = MagicMock(return_value=ok)

        with patch("nauro.cli.commands.auth.httpx.post") as mock_post:
            result = with_token_refresh(call)

        assert result.status_code == 200
        assert call.call_count == 1
        assert call.call_args.args == ("access_orig",)
        mock_post.assert_not_called()

    def test_401_triggers_refresh_then_retry_succeeds(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        unauthorized = MagicMock(spec=httpx.Response)
        unauthorized.status_code = 401
        ok = MagicMock(spec=httpx.Response)
        ok.status_code = 200
        call = MagicMock(side_effect=[unauthorized, ok])

        refresh_response = _mock_post(200, {"access_token": "access_new"})
        with patch("nauro.cli.commands.auth.httpx.post", return_value=refresh_response):
            result = with_token_refresh(call)

        assert result is ok
        assert call.call_count == 2
        # Retry uses the refreshed token, not the original
        assert call.call_args_list[0].args == ("access_orig",)
        assert call.call_args_list[1].args == ("access_new",)

    def test_401_then_refresh_failure_propagates(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        unauthorized = MagicMock(spec=httpx.Response)
        unauthorized.status_code = 401
        call = MagicMock(return_value=unauthorized)

        bad_refresh = _mock_post(400, {"error": "invalid_grant"})
        with (
            patch("nauro.cli.commands.auth.httpx.post", return_value=bad_refresh),
            pytest.raises(AuthRefreshError),
        ):
            with_token_refresh(call)

        # Stored tokens preserved despite the failed refresh attempt.
        auth = load_config()["auth"]
        assert auth["access_token"] == "access_orig"
        assert auth["refresh_token"] == "refresh_orig"

    def test_persistent_401_returns_response_no_infinite_loop(self):
        _seed_auth(refresh_token="refresh_orig", access_token="access_orig")

        unauthorized = MagicMock(spec=httpx.Response)
        unauthorized.status_code = 401
        call = MagicMock(return_value=unauthorized)

        refresh_response = _mock_post(200, {"access_token": "access_new"})
        with patch("nauro.cli.commands.auth.httpx.post", return_value=refresh_response):
            result = with_token_refresh(call)

        assert result.status_code == 401
        # Exactly two calls: initial + one retry. No further retries.
        assert call.call_count == 2

    def test_without_stored_token_raises(self):
        save_config({})

        call = MagicMock()
        with pytest.raises(AuthRefreshError, match="Not authenticated"):
            with_token_refresh(call)
        call.assert_not_called()
