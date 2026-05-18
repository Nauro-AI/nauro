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

from unittest.mock import MagicMock, patch

import httpx
import pytest

from nauro.cli.commands.auth import (
    AuthRefreshError,
    refresh_access_token,
    with_token_refresh,
)
from nauro.store.config import load_config, save_config


def _seed_auth(refresh_token: str = "refresh_orig", access_token: str = "access_orig") -> None:
    save_config(
        {
            "auth": {
                "sub": "auth0|test",
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        }
    )


def _mock_post(status_code: int = 200, payload: dict | None = None):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.text = str(payload or "")
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
