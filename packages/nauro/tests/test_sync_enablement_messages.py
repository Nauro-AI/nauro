"""Tests for sync-enablement guidance in `nauro status` and next-step output in `nauro auth login`.

status sync-line matrix
-----------------------
- local-only + no token  → mentions "local-only project" and "nauro link --cloud"
- local-only + token     → same (token presence must not change the message)
- cloud + no token       → mentions "nauro auth login"; must NOT mention "nauro link --cloud"
- cloud + token          → "active (event-driven, presign)"

auth login next steps
---------------------
The success path must print a block containing all four landmarks:
  nauro link --cloud, nauro sync, the connector URL, and the Codex port.
Failure/timeout paths must not print the block.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from nauro.cli.commands.auth import _CallbackHandler
from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_CLOUD, REPO_CONFIG_MODE_LOCAL
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

_CLOUD_PID = "01TESTCLOUDPID000000000001"
_LOCAL_PID = "01TESTLOCALPID000000000001"


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


def _setup_local_project(tmp_path, monkeypatch):
    _pid, store = register_project_v2(
        "localproj",
        [tmp_path],
        mode=REPO_CONFIG_MODE_LOCAL,
        project_id=_LOCAL_PID,
    )
    scaffold_project_store("localproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def _setup_cloud_project(tmp_path, monkeypatch):
    _pid, store = register_project_v2(
        "cloudproj",
        [tmp_path],
        mode=REPO_CONFIG_MODE_CLOUD,
        project_id=_CLOUD_PID,
        server_url="https://example.test",
    )
    scaffold_project_store("cloudproj", store)
    monkeypatch.chdir(tmp_path)
    return store


def _inject_token(token: str = "tok_test") -> None:
    save_config({"auth": {"access_token": token, "sub": "auth0|testuser"}})


# ---------------------------------------------------------------------------
# status sync-line matrix
# ---------------------------------------------------------------------------


class TestStatusSyncLine:
    def test_local_only_no_token(self, tmp_path, monkeypatch):
        _setup_local_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "local-only project" in result.output
        assert "nauro link --cloud" in result.output

    def test_local_only_with_token(self, tmp_path, monkeypatch):
        _setup_local_project(tmp_path, monkeypatch)
        _inject_token()

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "local-only project" in result.output
        assert "nauro link --cloud" in result.output

    def test_cloud_no_token(self, tmp_path, monkeypatch):
        _setup_cloud_project(tmp_path, monkeypatch)

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "nauro auth login" in result.output
        assert "nauro link --cloud" not in result.output

    def test_cloud_with_token(self, tmp_path, monkeypatch):
        _setup_cloud_project(tmp_path, monkeypatch)
        _inject_token()

        with patch("nauro.cli.commands.status._count_remote_decisions", return_value=1):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "active (event-driven, presign)" in result.output


# ---------------------------------------------------------------------------
# auth login next steps
# ---------------------------------------------------------------------------


_HTTPServer = __import__("http.server", fromlist=["HTTPServer"]).HTTPServer


def _fake_server_init(self, addr, handler_class):
    pass


def _fake_server_close(self):
    pass


class TestAuthLoginNextSteps:
    def _token_response(self):
        fake_token = _make_jwt({"sub": "auth0|user123"})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": fake_token,
            "refresh_token": "refresh_xyz",
        }
        resp.raise_for_status = MagicMock()
        return resp

    def _me_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"user_id": "usr_test", "email": "test@example.com"}
        resp.raise_for_status = MagicMock()
        return resp

    def test_success_prints_next_steps_block(self, tmp_path, monkeypatch):
        token_resp = self._token_response()
        me_resp = self._me_response()

        def fake_post(url, **kwargs):
            return token_resp

        def fake_get(url, **kwargs):
            return me_resp

        def fake_handle_request(self):
            _CallbackHandler.auth_code = "AUTH_CODE_TEST"
            _CallbackHandler.error = None

        with (
            patch("nauro.cli.commands.auth.httpx.post", side_effect=fake_post),
            patch("nauro.cli.commands.auth.httpx.get", side_effect=fake_get),
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(_HTTPServer, "__init__", _fake_server_init),
            patch.object(_HTTPServer, "handle_request", fake_handle_request),
            patch.object(_HTTPServer, "server_close", _fake_server_close),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 0
        assert "nauro link --cloud" in result.output
        assert "nauro sync" in result.output
        assert "https://mcp.nauro.ai/mcp" in result.output
        assert "mcp_oauth_callback_port = 8765" in result.output

    def test_timeout_does_not_print_next_steps(self, tmp_path, monkeypatch):
        def fake_handle_request(self):
            _CallbackHandler.auth_code = None
            _CallbackHandler.error = None

        with (
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(_HTTPServer, "__init__", _fake_server_init),
            patch.object(_HTTPServer, "handle_request", fake_handle_request),
            patch.object(_HTTPServer, "server_close", _fake_server_close),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "nauro link --cloud" not in result.output
        assert "mcp_oauth_callback_port" not in result.output

    def test_callback_error_does_not_print_next_steps(self, tmp_path, monkeypatch):
        def fake_handle_request(self):
            _CallbackHandler.auth_code = None
            _CallbackHandler.error = "access_denied"

        with (
            patch("nauro.cli.commands.auth.webbrowser.open"),
            patch.object(_HTTPServer, "__init__", _fake_server_init),
            patch.object(_HTTPServer, "handle_request", fake_handle_request),
            patch.object(_HTTPServer, "server_close", _fake_server_close),
        ):
            result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 1
        assert "nauro link --cloud" not in result.output
        assert "mcp_oauth_callback_port" not in result.output
