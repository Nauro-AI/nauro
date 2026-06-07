"""Tests for nauro auth — Authorization Code + PKCE flow."""

import base64
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from nauro_core import sanitize_sub
from typer.testing import CliRunner

from nauro.cli.commands import auth as auth_module
from nauro.cli.commands.auth import (
    _CallbackHandler,
    _decode_jwt_payload,
    _generate_pkce,
)
from nauro.cli.main import app
from nauro.store.config import load_config, save_config

runner = CliRunner()


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT (header.payload.signature) with the given payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


# --- sanitize_sub wiring ---
# Behavior coverage lives in nauro-core's test_identity.py; this only pins that
# the auth command derives its S3 key prefix from the canonical implementation.


def test_auth_uses_canonical_sanitize_sub():
    assert auth_module.sanitize_sub is sanitize_sub


# --- _decode_jwt_payload ---


class TestDecodeJwt:
    def test_valid_jwt(self):
        token = _make_jwt({"sub": "auth0|abc123", "aud": "test"})
        payload = _decode_jwt_payload(token)
        assert payload["sub"] == "auth0|abc123"
        assert payload["aud"] == "test"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("not-a-jwt")

    def test_padding_handling(self):
        """JWT base64 segments may lack padding — decoder must handle it."""
        token = _make_jwt({"sub": "x"})
        payload = _decode_jwt_payload(token)
        assert payload["sub"] == "x"


# --- _generate_pkce ---


class TestGeneratePkce:
    def test_returns_verifier_and_challenge(self):
        verifier, challenge = _generate_pkce()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)
        assert len(verifier) > 40
        assert len(challenge) > 20

    def test_challenge_is_s256_of_verifier(self):
        import hashlib

        verifier, challenge = _generate_pkce()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_unique_each_call(self):
        v1, _ = _generate_pkce()
        v2, _ = _generate_pkce()
        assert v1 != v2


# --- auth login (mocked PKCE flow) ---


def _simulate_callback_success(auth_code: str = "AUTH_CODE_123"):
    """Simulate the browser callback by setting handler state directly."""
    _CallbackHandler.auth_code = auth_code
    _CallbackHandler.error = None


def _simulate_callback_error(error: str = "access_denied"):
    """Simulate a failed browser callback."""
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = error


def test_callback_page_escapes_reflected_message():
    """The loopback callback page escapes its message so an Auth0-supplied
    error_description reflected from the redirect cannot inject markup."""
    from nauro.cli.commands.auth import _callback_page

    page = _callback_page("<script>alert(1)</script>")
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


class TestAuthLogin:
    def test_login_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NAURO_API_URL", "https://test.api.example.com")

        fake_token = _make_jwt({"sub": "auth0|user123"})

        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": fake_token,
            "refresh_token": "refresh_xyz",
            "token_type": "Bearer",
        }
        token_response.raise_for_status = MagicMock()

        def fake_post(url, **kwargs):
            return token_response

        # Patch the server to not actually listen, simulate callback inline
        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_success()

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.httpx.post", side_effect=fake_post),
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 0
        assert "Authenticated as auth0|user123" in result.output

        # Verify config was written
        config = load_config()
        assert config["auth"]["sub"] == "auth0|user123"
        assert config["auth"]["sanitized_sub"] == "auth0-user123"
        assert config["auth"]["access_token"] == fake_token
        assert config["auth"]["refresh_token"] == "refresh_xyz"

    def test_login_callback_error(self, tmp_path, monkeypatch):

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_error("access_denied")

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "access_denied" in result.output

    def test_login_timeout(self, tmp_path, monkeypatch):

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            # Simulate timeout — don't set auth_code or error
            _CallbackHandler.auth_code = None
            _CallbackHandler.error = None

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "timed out" in result.output

    def test_login_token_exchange_failure(self, tmp_path, monkeypatch):

        def fake_post(url, **kwargs):
            raise httpx.HTTPStatusError(
                "403 Forbidden",
                request=MagicMock(),
                response=MagicMock(status_code=403),
            )

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_success()

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.httpx.post", side_effect=fake_post),
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "Token exchange failed" in result.output

    def test_login_429_then_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NAURO_API_URL", "https://test.api.example.com")

        fake_token = _make_jwt({"sub": "auth0|user123"})

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}

        ok = MagicMock()
        ok.status_code = 200
        ok.headers = {}
        ok.json.return_value = {
            "access_token": fake_token,
            "refresh_token": "refresh_xyz",
            "token_type": "Bearer",
        }
        ok.raise_for_status = MagicMock()

        post = MagicMock(side_effect=[rate_limited, ok])

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_success()

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep"),
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 0
        assert post.call_count == 2
        config = load_config()
        assert config["auth"]["sub"] == "auth0|user123"
        assert config["auth"]["access_token"] == fake_token

    def test_login_429_exhausted(self, tmp_path, monkeypatch):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}
        post = MagicMock(return_value=rate_limited)

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_success()

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep"),
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "rate-limiting" in result.output
        assert post.call_count == 3

    def test_login_non_429_failure_does_not_retry(self, tmp_path, monkeypatch):
        bad = MagicMock()
        bad.status_code = 400
        bad.headers = {}
        bad.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "400 Bad Request",
                request=MagicMock(),
                response=MagicMock(status_code=400),
            )
        )
        post = MagicMock(return_value=bad)

        def fake_server_init(self, addr, handler_class):
            pass

        def fake_handle_request(self):
            _simulate_callback_success()

        def fake_server_close(self):
            pass

        with (
            patch("nauro.cli.commands.auth.httpx.post", post),
            patch("nauro.cli.commands.auth.time.sleep") as sleep,
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "__init__",
                fake_server_init,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "handle_request",
                fake_handle_request,
            ),
            patch.object(
                __import__("http.server", fromlist=["HTTPServer"]).HTTPServer,
                "server_close",
                fake_server_close,
            ),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert post.call_count == 1
        sleep.assert_not_called()


# --- auth status ---


class TestAuthStatus:
    def test_status_authenticated(self, tmp_path, monkeypatch):
        save_config(
            {
                "auth": {
                    "sub": "auth0|abc123",
                    "sanitized_sub": "auth0-abc123",
                    "access_token": "tok_123",
                    "refresh_token": "ref_456",
                }
            }
        )
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "Authenticated as: auth0|abc123" in result.output
        assert "Sanitized sub:    auth0-abc123" in result.output
        assert "Refresh token:    yes" in result.output

    def test_status_not_authenticated(self, tmp_path, monkeypatch):
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_status_no_refresh_token(self, tmp_path, monkeypatch):
        save_config(
            {
                "auth": {
                    "sub": "auth0|abc123",
                    "sanitized_sub": "auth0-abc123",
                    "access_token": "tok_123",
                }
            }
        )
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "Refresh token:    no" in result.output


# --- auth logout ---


class TestAuthLogout:
    def test_logout(self, tmp_path, monkeypatch):
        save_config(
            {
                "api_key": "sk-keep-this",
                "auth": {
                    "sub": "auth0|abc123",
                    "sanitized_sub": "auth0-abc123",
                    "access_token": "tok_123",
                },
            }
        )
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output

        # Auth should be gone, other config preserved
        config = load_config()
        assert "auth" not in config
        assert config["api_key"] == "sk-keep-this"

    def test_logout_not_authenticated(self, tmp_path, monkeypatch):
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Not authenticated" in result.output

    def test_config_permissions(self, tmp_path, monkeypatch):
        """Config file should have restricted permissions (0o600) after auth write."""
        import os

        save_config(
            {
                "auth": {
                    "sub": "auth0|abc123",
                    "sanitized_sub": "auth0-abc123",
                    "access_token": "tok_123",
                }
            }
        )
        config_path = tmp_path / "config.json"
        mode = os.stat(config_path).st_mode & 0o777
        assert mode == 0o600


# --- shipped defaults + resolver (regression lock-in for the empty-defaults bug) ---


def test_defaults_are_non_empty():
    """Trip-wire: a future audit pass must not strip these back to empty strings."""
    from nauro.cli.commands.auth import (
        DEFAULT_API_URL,
        DEFAULT_AUTH0_AUDIENCE,
        DEFAULT_AUTH0_CLIENT_ID,
        DEFAULT_AUTH0_DOMAIN,
    )

    assert DEFAULT_AUTH0_DOMAIN
    assert DEFAULT_AUTH0_CLIENT_ID
    assert DEFAULT_API_URL
    assert DEFAULT_AUTH0_AUDIENCE


class TestResolveAuthConfig:
    """Pure-function resolver tests — no monkeypatching, no tmp_path."""

    def _call(self, env=None, config=None):
        from nauro.cli.commands.auth import _resolve_auth_config

        return _resolve_auth_config(env or {}, config or {})

    def _defaults(self):
        from nauro.cli.commands.auth import (
            DEFAULT_API_URL,
            DEFAULT_AUTH0_AUDIENCE,
            DEFAULT_AUTH0_CLIENT_ID,
            DEFAULT_AUTH0_DOMAIN,
        )

        return (
            DEFAULT_AUTH0_DOMAIN,
            DEFAULT_AUTH0_CLIENT_ID,
            DEFAULT_API_URL,
            DEFAULT_AUTH0_AUDIENCE,
        )

    def test_empty_env_empty_config_returns_defaults(self):
        domain, client_id, api_url, audience = self._call()
        assert (domain, client_id, api_url, audience) == self._defaults()

    def test_config_complete_pair_returns_config(self):
        cfg = {"auth0_domain": "cfg.auth0.com", "auth0_client_id": "cfg-id"}
        domain, client_id, _, _ = self._call(config=cfg)
        assert domain == "cfg.auth0.com"
        assert client_id == "cfg-id"

    def test_config_only_domain_raises(self):
        from nauro.cli.commands.auth import PartialAuthConfigError

        with pytest.raises(PartialAuthConfigError):
            self._call(config={"auth0_domain": "cfg.auth0.com"})

    def test_config_only_client_id_raises(self):
        from nauro.cli.commands.auth import PartialAuthConfigError

        with pytest.raises(PartialAuthConfigError):
            self._call(config={"auth0_client_id": "cfg-id"})

    def test_env_complete_pair_overrides_config(self):
        env = {
            "NAURO_AUTH0_DOMAIN": "env.auth0.com",
            "NAURO_AUTH0_CLIENT_ID": "env-id",
        }
        cfg = {"auth0_domain": "cfg.auth0.com", "auth0_client_id": "cfg-id"}
        domain, client_id, _, _ = self._call(env=env, config=cfg)
        assert domain == "env.auth0.com"
        assert client_id == "env-id"

    def test_env_only_domain_raises(self):
        from nauro.cli.commands.auth import PartialAuthConfigError

        with pytest.raises(PartialAuthConfigError):
            self._call(env={"NAURO_AUTH0_DOMAIN": "env.auth0.com"})

    def test_env_only_client_id_raises(self):
        from nauro.cli.commands.auth import PartialAuthConfigError

        with pytest.raises(PartialAuthConfigError):
            self._call(env={"NAURO_AUTH0_CLIENT_ID": "env-id"})

    def test_api_url_env_overrides_config_and_default(self):
        _, _, api_url, _ = self._call(
            env={"NAURO_API_URL": "https://env.example/"},
            config={"api_url": "https://cfg.example/"},
        )
        assert api_url == "https://env.example/"

    def test_api_url_config_overrides_default(self):
        _, _, api_url, _ = self._call(config={"api_url": "https://cfg.example/"})
        assert api_url == "https://cfg.example/"

    def test_api_url_falls_back_to_default(self):
        _, _, api_url, _ = self._call()
        _, _, expected, _ = self._defaults()
        assert api_url == expected

    def test_audience_env_overrides_config_and_default(self):
        _, _, _, audience = self._call(
            env={"NAURO_AUTH0_AUDIENCE": "https://env.aud/mcp"},
            config={"auth0_audience": "https://cfg.aud/mcp"},
        )
        assert audience == "https://env.aud/mcp"

    def test_audience_config_overrides_default(self):
        _, _, _, audience = self._call(config={"auth0_audience": "https://cfg.aud/mcp"})
        assert audience == "https://cfg.aud/mcp"

    def test_audience_falls_back_to_default(self):
        _, _, _, audience = self._call()
        _, _, _, expected = self._defaults()
        assert audience == expected
