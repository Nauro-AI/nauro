"""Invariants for ``nauro.sync.remote.resolve_api_url``.

Every caller appends a path like ``/sync/manifest`` to the result, so the
function must strip trailing slashes regardless of which precedence
branch supplies the URL.
"""

from __future__ import annotations

from nauro.store.config import save_config
from nauro.sync.remote import resolve_api_url


def test_strips_trailing_slash_from_env_var(monkeypatch):
    monkeypatch.setenv("NAURO_API_URL", "https://mcp.example.test/")
    assert resolve_api_url() == "https://mcp.example.test"


def test_strips_trailing_slash_from_config(monkeypatch, tmp_path):
    monkeypatch.delenv("NAURO_API_URL", raising=False)
    save_config({"api_url": "https://mcp.example.test/"})
    assert resolve_api_url() == "https://mcp.example.test"


def test_env_var_wins_over_config(monkeypatch, tmp_path):
    monkeypatch.setenv("NAURO_API_URL", "https://env.example.test")
    save_config({"api_url": "https://config.example.test"})
    assert resolve_api_url() == "https://env.example.test"
