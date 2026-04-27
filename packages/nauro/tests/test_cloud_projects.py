"""Tests for nauro.sync.cloud_projects HTTP client."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from nauro.store.config import save_config
from nauro.sync import cloud_projects
from nauro.sync.cloud_projects import (
    CloudProjectError,
    create_project,
    list_projects,
)


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "nauro_home"))


def _seed_token(monkeypatch, tmp_path, token: str = "test-token") -> None:
    """Write a config.json with an OAuth access token, mirroring `nauro auth login`."""
    _patch_home(monkeypatch, tmp_path)
    save_config({"auth": {"access_token": token, "sub": "auth0|test"}})


def _stub_request(handler):
    """Patch httpx.request inside cloud_projects with `handler(method, url, **kwargs)`."""
    return patch.object(cloud_projects.httpx, "request", side_effect=handler)


# ── create_project ────────────────────────────────────────────────────────────


def test_create_project_success_sends_bearer_and_body(tmp_path, monkeypatch):
    """POST /projects with a JSON body and Authorization header; parses response."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    captured: dict = {}

    def handler(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return httpx.Response(
            201,
            json={
                "project_id": "01KQ6AZGNA0B3QBF67NBXP3S45",
                "name": "demo",
                "role": "owner",
                "created_at": "2026-04-27T10:11:12Z",
            },
            request=httpx.Request(method, url),
        )

    with _stub_request(handler):
        view = create_project("demo")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.test/projects"
    assert captured["json"] == {"name": "demo"}
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert view == {
        "project_id": "01KQ6AZGNA0B3QBF67NBXP3S45",
        "name": "demo",
        "role": "owner",
        "created_at": "2026-04-27T10:11:12Z",
    }


def test_create_project_auth_failure_renders_message(tmp_path, monkeypatch):
    """A 401 raises CloudProjectError with a clear, user-renderable message."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(401, json={"detail": "expired"}, request=httpx.Request(method, url))

    with _stub_request(handler):
        with pytest.raises(CloudProjectError) as exc:
            create_project("demo")
    msg = str(exc.value)
    assert "401" in msg
    assert "nauro auth login" in msg


def test_create_project_server_error_renders_message(tmp_path, monkeypatch):
    """A 5xx raises CloudProjectError noting the server failure."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(503, request=httpx.Request(method, url))

    with _stub_request(handler):
        with pytest.raises(CloudProjectError) as exc:
            create_project("demo")
    assert "503" in str(exc.value)


def test_create_project_network_error_renders_message(tmp_path, monkeypatch):
    """Transport-level failures surface as CloudProjectError, not raw httpx errors."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        raise httpx.ConnectError("dns failure")

    with _stub_request(handler):
        with pytest.raises(CloudProjectError) as exc:
            create_project("demo")
    assert "Network error" in str(exc.value)


def test_no_token_raises_before_request(tmp_path, monkeypatch):
    """Without a stored token, the client refuses to issue a request."""
    _patch_home(monkeypatch, tmp_path)
    # No save_config call → no auth section.
    with pytest.raises(CloudProjectError) as exc:
        create_project("demo")
    assert "nauro auth login" in str(exc.value)


# ── list_projects ─────────────────────────────────────────────────────────────


def test_list_projects_empty(tmp_path, monkeypatch):
    """GET /projects with no projects → empty list."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    with _stub_request(handler):
        result = list_projects()
    assert result == []


def test_list_projects_preserves_server_order(tmp_path, monkeypatch):
    """Two projects come back in the order the server returned them."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    server_payload = [
        {
            "project_id": "01KQ6AZGNA0B3QBF67NBXP3S45",
            "name": "first",
            "role": "owner",
            "created_at": "2026-04-01T00:00:00Z",
        },
        {
            "project_id": "01KQ7BZGZA0B3QBF67NBXP3S99",
            "name": "second",
            "role": "viewer",
            "created_at": "2026-04-15T00:00:00Z",
        },
    ]

    def handler(method, url, **kwargs):
        assert method == "GET"
        assert url == "https://example.test/projects"
        return httpx.Response(200, json=server_payload, request=httpx.Request(method, url))

    with _stub_request(handler):
        result = list_projects()

    assert [p["project_id"] for p in result] == [
        "01KQ6AZGNA0B3QBF67NBXP3S45",
        "01KQ7BZGZA0B3QBF67NBXP3S99",
    ]
    assert result[0]["name"] == "first"
    assert result[1]["role"] == "viewer"


def test_list_projects_accepts_wrapped_envelope(tmp_path, monkeypatch):
    """Server may wrap as {"projects": [...]}; client tolerates either shape."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "projects": [
                    {
                        "project_id": "01KQ6AZGNA0B3QBF67NBXP3S45",
                        "name": "wrapped",
                        "role": "owner",
                        "created_at": "2026-04-27T00:00:00Z",
                    }
                ]
            },
            request=httpx.Request(method, url),
        )

    with _stub_request(handler):
        result = list_projects()
    assert len(result) == 1
    assert result[0]["name"] == "wrapped"


def test_default_api_url_used_when_env_absent(tmp_path, monkeypatch):
    """With no NAURO_API_URL and no config api_url, the public default is used."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.delenv("NAURO_API_URL", raising=False)

    captured: dict = {}

    def handler(method, url, **kwargs):
        captured["url"] = url
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    with _stub_request(handler):
        list_projects()

    # DEFAULT_API_URL == https://mcp.nauro.ai (mirrored from auth.py)
    assert captured["url"] == "https://mcp.nauro.ai/projects"


def test_malformed_project_payload_raises(tmp_path, monkeypatch):
    """A response missing required fields surfaces as CloudProjectError."""
    _seed_token(monkeypatch, tmp_path)
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")

    def handler(method, url, **kwargs):
        return httpx.Response(
            201,
            content=json.dumps({"project_id": "x", "name": "y"}).encode(),
            headers={"Content-Type": "application/json"},
            request=httpx.Request(method, url),
        )

    with _stub_request(handler):
        with pytest.raises(CloudProjectError) as exc:
            create_project("demo")
    assert "missing required field" in str(exc.value)
