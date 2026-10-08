"""Committed mutation receipts retain the latest refresh attempt."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nauro.sync import generation_refresh_status as status
from nauro.sync import generation_writes as writes
from tests.test_generation_write_delivery import CASES
from tests.test_stdio_startup_authority import installed

__all__ = ["installed"]


@pytest.mark.parametrize("operation,content", CASES)
@pytest.mark.parametrize("failed", [False, True])
def test_committed_write_records_current_refresh_attempt(
    installed, monkeypatch, operation, content, failed
):
    _, binding, _, _, _, _, _ = installed
    earlier = "2026-09-01T00:00:00.000000Z"
    current = "2026-09-01T00:01:00.000000Z"
    refresh = Mock(return_value=object())
    monkeypatch.setattr(status, "prepare_generation_refresh", refresh)
    monkeypatch.setattr(status, "commit_generation_refresh", Mock(return_value=object()))
    monkeypatch.setattr(status, "_now", lambda: earlier)
    status.refresh_replica(binding)
    monkeypatch.setattr(status, "_now", lambda: current)
    if failed:
        refresh.side_effect = OSError("refresh unavailable")
    family = writes.FAMILIES[operation]
    submission = importlib.import_module(f"nauro.sync.{family}_submission")
    receipt = {"status": "committed", "receipt_json": '{"verified":"receipt"}'}
    submit = Mock(return_value=SimpleNamespace(status="committed", model_dump=lambda **kw: receipt))
    monkeypatch.setattr(submission, f"submit_{family}", submit)

    result = writes.generation_write(operation, {"project_id": binding.project_id, **content})

    assert result["status"] == "committed"
    assert result["receipt_json"] == receipt["receipt_json"]
    retained = status.replica_status(binding)
    assert retained["last_refresh_attempt_at"] == current
    assert retained["last_refresh_succeeded_at"] == (earlier if failed else current)
    assert retained["last_refresh_error_code"] == ("refresh_failed" if failed else None)
    if failed:
        assert result["replica_status"]["error_code"] == "receipt_refresh_required"
    else:
        assert result["replica_status"] == retained
    submit.assert_called_once()
