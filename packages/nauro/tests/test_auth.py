"""Tests for nauro auth — Authorization Code + PKCE flow."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nauro.cli.commands.auth import (
    _CallbackHandler,
    _decode_jwt_payload,
    _generate_pkce,
    _sanitize_sub,
)
from nauro.cli.main import app
from nauro.store.config import load_config, save_config

runner = CliRunner()


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT (header.payload.signature) with the given payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


# --- _sanitize_sub ---


class TestSanitizeSub:
    def test_pipe_replaced(self):
        assert _sanitize_sub("auth0|abc123") == "auth0-abc123"

    def test_google_oauth2(self):
        assert _sanitize_sub("google-oauth2|456def") == "google-oauth2-456def"

    def test_already_safe(self):
        assert _sanitize_sub("abc-def_123") == "abc-def_123"

    def test_truncation_at_128(self):
        long_sub = "a" * 200
        assert len(_sanitize_sub(long_sub)) == 128

    def test_special_characters(self):
        assert _sanitize_sub("user@example.com") == "user-example-com"

    def test_empty_string(self):
        assert _sanitize_sub("") == ""


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


def _patch_auth_config(monkeypatch):
    """Set auth constants that no longer have hardcoded defaults."""
    monkeypatch.setattr("nauro.cli.commands.auth.AUTH0_DOMAIN", "test.auth0.com")
    monkeypatch.setattr("nauro.cli.commands.auth.AUTH0_CLIENT_ID", "test-client-id")
    monkeypatch.setattr("nauro.cli.commands.auth.AUTH0_AUDIENCE", "https://test.api/mcp")


class TestAuthLogin:
    def test_login_success(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path)
        _patch_auth_config(monkeypatch)
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
        _patch_home(monkeypatch, tmp_path)
        _patch_auth_config(monkeypatch)

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
        _patch_home(monkeypatch, tmp_path)
        _patch_auth_config(monkeypatch)

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
        _patch_home(monkeypatch, tmp_path)
        _patch_auth_config(monkeypatch)

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

        import httpx

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


# --- auth status ---


class TestAuthStatus:
    def test_status_authenticated(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path)
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
        assert "auth0|abc123" in result.output
        assert "auth0-abc123" in result.output
        assert "yes" in result.output  # refresh token present

    def test_status_not_authenticated(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path)
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_status_no_refresh_token(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path)
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
        assert "no" in result.output  # no refresh token


# --- auth logout ---


class TestAuthLogout:
    def test_logout(self, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tmp_path)
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
        _patch_home(monkeypatch, tmp_path)
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Not authenticated" in result.output

    def test_config_permissions(self, tmp_path, monkeypatch):
        """Config file should have restricted permissions (0o600) after auth write."""
        import os

        _patch_home(monkeypatch, tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|abc123",
                    "sanitized_sub": "auth0-abc123",
                    "access_token": "tok_123",
                }
            }
        )
        config_path = tmp_path / "nauro_home" / "config.json"
        mode = os.stat(config_path).st_mode & 0o777
        assert mode == 0o600
