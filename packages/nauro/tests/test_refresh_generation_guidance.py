import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import stdio_server
from nauro.sync import generation_decision as delivery
from nauro.sync import generation_guidance as admission
from nauro.sync.generation_refresh_status import refresh_replica, replica_status
from nauro.templates import generation_guidance as guidance
from tests.test_stdio_startup_authority import installed as installed_fixture


@pytest.fixture
def installed(tmp_path, monkeypatch):
    return installed_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("entry", ["sync", "startup", "receipt"])
def test_refresh_guidance_uses_returned_snapshot_without_capture(installed, monkeypatch, entry):
    repo, binding, connection, _, _, calls, _ = installed
    capture = Mock(side_effect=AssertionError("Second replica capture"))
    monkeypatch.setattr(admission, "admit_generation_store", capture)
    calls.clear()
    if entry == "sync":
        result = CliRunner().invoke(app, ["sync", "--project", binding.project_id])
        assert result.exit_code == 0, result.output
    elif entry == "startup":
        stdio_server._pull_on_startup()
    else:
        receipt = '{"exact":"receipt"}'
        response = {"status": "committed", "execution": {"receipt_json": receipt}}
        transport = Mock(return_value=response)
        monkeypatch.setattr(delivery.DecisionReferenceTransport, "propose_decision", transport)
        result = delivery.execute_decision(
            (connection, binding.project_id),
            {"request_mode": "submit"},
            on_refreshed=guidance.regenerate_refreshed_guidance,
        )
        assert result["execution"]["receipt_json"] == receipt
        assert result["guidance_status"] == {
            "status": "completed",
            "updated_repos": 1,
            "warnings": [],
        }
        transport.assert_called_once_with(request_mode="submit")
    saved = (repo / "AGENTS.md").read_text()
    assert "Derived context from generation" in saved
    capture.assert_not_called()
    assert [request.url.path for request in calls] == ["/generations/projection"] * 5
    assert replica_status(binding)["last_refresh_error_code"] is None


@pytest.mark.parametrize("entry", ["sync", "startup", "receipt"])
def test_guidance_write_failure_preserves_refresh_and_receipt(
    installed, monkeypatch, caplog, entry
):
    repo, binding, connection, _, _, _, _ = installed
    saved = repo / "AGENTS.md"
    saved.write_text("Preserved owner guidance\n")
    monkeypatch.setattr(guidance, "warn_then_regen", Mock(side_effect=OSError("PRIVATE FAILURE")))
    if entry == "sync":
        result = CliRunner().invoke(app, ["sync", "--project", binding.project_id])
        assert result.exit_code == 0
        assert "regeneration failed" in result.stderr
        assert "PRIVATE" not in result.output
    elif entry == "startup":
        stdio_server._pull_on_startup()
        assert "regeneration failed" in caplog.text
        assert "session-start refresh: incomplete" not in caplog.text
        assert "PRIVATE" not in caplog.text
    else:
        receipt = '{"exact":"receipt"}'
        response = {"status": "committed", "execution": {"receipt_json": receipt}}
        transport = Mock(return_value=response)
        monkeypatch.setattr(delivery.DecisionReferenceTransport, "propose_decision", transport)
        result = delivery.execute_decision(
            (connection, binding.project_id),
            {"request_mode": "submit"},
            on_refreshed=guidance.regenerate_refreshed_guidance,
        )
        assert result["status"] == "committed"
        assert result["execution"]["receipt_json"] == receipt
        assert result["guidance_status"]["status"] == "failed"
        assert result["replica_status"]["last_refresh_error_code"] is None
        assert "PRIVATE" not in json.dumps(result)
        transport.assert_called_once_with(request_mode="submit")
    assert replica_status(binding)["last_refresh_error_code"] is None
    assert saved.read_text() == "Preserved owner guidance\n"


@pytest.mark.parametrize("change", ["revoked", "expired", "actor"])
def test_reused_snapshot_requires_current_authority(installed, change):
    repo, binding, _, credentials, control, calls, _ = installed
    snapshot = refresh_replica(binding)
    before = replica_status(binding)
    (repo / "AGENTS.md").write_text("Preserved guidance\n")
    if change == "revoked":
        control["status"] = 403
    else:
        with credentials.locked():
            record = credentials.read()
            delta = (
                {"expires_at": 1}
                if change == "expired"
                else {"user_id": "01K88888888888888888888888"}
            )
            credentials.write(record.model_copy(update=delta))
    calls.clear()
    result = guidance.regenerate_refreshed_guidance(snapshot)
    assert result["status"] == "failed"
    assert (repo / "AGENTS.md").read_text() == "Preserved guidance\n"
    if change == "revoked":
        assert (
            replica_status(binding)["last_refresh_succeeded_at"]
            == before["last_refresh_succeeded_at"]
        )
    assert all(request.url.path == "/generations/projection" for request in calls)


def test_unchanged_guidance_preserves_file_bytes_and_mtime(installed):
    repo, binding, _, _, _, _, _ = installed
    snapshot = refresh_replica(binding)
    assert guidance.regenerate_refreshed_guidance(snapshot)["updated_repos"] == 1
    path = repo / "AGENTS.md"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert guidance.regenerate_refreshed_guidance(snapshot)["updated_repos"] == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
