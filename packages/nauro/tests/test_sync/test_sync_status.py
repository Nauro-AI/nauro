"""Tests for ``nauro sync --status`` reporting.

After the legacy-transport removal, status is a two-state report:
authenticated → server URL + per-project sync info; not authenticated →
"run nauro auth login" guidance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.config import save_config
from nauro.store.registry import register_project
from nauro.sync.state import SyncState, save_state
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _scaffold(name: str = "statusproj", *, repo):
    store = register_project(name, [repo])
    scaffold_project_store(name, store)
    return store


class TestSyncPathReporting:
    def test_auth_token_reports_authenticated(self, tmp_path, monkeypatch):
        _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "authenticated (presign)" in result.output
        assert "Server:" in result.output

    def test_no_credentials_reports_not_authenticated(self, tmp_path, monkeypatch):
        save_config({})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "not authenticated" in result.output
        assert "nauro auth login" in result.output


class TestLastSyncTime:
    def test_reports_last_full_sync_when_present(self, tmp_path, monkeypatch):
        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        stamp = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        state = SyncState(last_full_sync=stamp)
        save_state(store, state)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert stamp in result.output

    def test_reports_never_when_absent(self, tmp_path, monkeypatch):
        _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Last successful sync: never" in result.output
