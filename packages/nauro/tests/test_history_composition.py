from __future__ import annotations

import ast
import inspect
from pathlib import Path

import httpx
import pytest

from nauro.auth import ActiveCredentials
from nauro.mcp import generation_responses as responses
from nauro.mcp import read_dispatch as dispatch
from nauro.store import generation_installation as installation
from nauro.sync import generation_refresh as refresh
from nauro.sync import history_transport as transport
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import POSIX
from tests.test_generation_reads import admitted as admitted
from tests.test_history_transport import OTHER, response_body
from tests.test_read_dispatch import cloud as cloud
from tests.test_read_dispatch import isolated_home as isolated_home


@POSIX
def test_response_and_dispatch_keep_authority_without_private_wire_fields(cloud, monkeypatch):
    binding, current, _ = cloud
    target = current[0].target
    monkeypatch.setattr(
        transport, "read_active_credentials", lambda: ActiveCredentials(USER_ID, "token")
    )

    def handler(request):
        return httpx.Response(200, json=response_body(target, 7))

    snapshots = binding.store_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "001.json").write_text("POISON HISTORY")
    before = {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(
        dispatch.legacy,
        "tool_diff_since_last_session",
        lambda *a, **k: pytest.fail("Flat history read"),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = transport.HttpHistoryTransport(binding.server_url, client)
        result = responses.diff_since_last_session(binding, 7, actor=USER_ID, transport=adapter)
        composed = dispatch.history_dispatch(adapter)
        actual = composed(project_id=binding.project_id, days=7)
    assert result.is_error is False
    assert result.envelope == {
        "store": "local",
        "project": {"id": binding.project_id, "name": binding.project_id},
        "diff": response_body(target, 7)["diff"],
        "read_authority": response_body(target, 7)["read_authority"],
        "cutoff_date_used": "2026-09-01T00:00:00+00:00",
    }
    assert actual.isError is False
    assert actual.content[0].text == result.text
    assert actual.structuredContent is None
    assert USER_ID not in repr(result)
    assert target.identity.projection_scope_id not in repr(result)
    assert "POISON" not in repr(result)
    assert {
        p.relative_to(binding.store_path): p.read_bytes()
        for p in binding.store_path.rglob("*")
        if p.is_file()
    } == before
    assert inspect.signature(composed) == inspect.signature(dispatch.diff_since_last_session)


@POSIX
@pytest.mark.parametrize(
    "fault", ["scope", "advance", "account", "pointer", "intent", "barrier", "network"]
)
def test_changes_during_history_discard_result(admitted, monkeypatch, fault):
    binding, current, _ = admitted
    target = current[0].target
    monkeypatch.setattr(
        transport, "read_active_credentials", lambda: ActiveCredentials(USER_ID, "token")
    )

    def handler(request):
        if fault == "scope":
            identity = target.identity.model_copy(update={"projection_scope_id": "b" * 64})
            monkeypatch.setattr(
                refresh,
                "check_generation_projection",
                lambda *a, **k: transport.GenerationProjectionTarget(binding, identity),
            )
        elif fault == "advance":
            from tests.test_generation_refresh import _target

            current[0] = _target()
            refresh.recover_generation_refresh(binding, actor=USER_ID)
        elif fault == "account":
            monkeypatch.setattr(installation, "read_active_user_id", lambda: OTHER)
        elif fault == "pointer":
            paths = refresh.refresh_paths(binding, USER_ID)
            paths.pointer.write_bytes(b"corrupt")
        elif fault == "intent":
            refresh.refresh_paths(binding, USER_ID).intent.unlink()
        elif fault == "barrier":

            def fail(*a, **k):
                raise OSError("PRIVATE")

            monkeypatch.setattr(refresh, "sync_file", fail)
        else:
            raise httpx.ReadError("PRIVATE")
        return httpx.Response(200, json=response_body(target))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = responses.diff_since_last_session(
            binding,
            actor=USER_ID,
            transport=transport.HttpHistoryTransport(binding.server_url, client),
        )
    assert result.is_error is True
    assert set(result.envelope) == {"store", "error"}
    assert "Removed file" not in result.text
    assert "PRIVATE" not in result.text


@POSIX
@pytest.mark.parametrize("fault", ["stale", "intent", "origin", "days"])
def test_failed_admission_never_requests_or_repairs(admitted, monkeypatch, fault):
    binding, current, _ = admitted
    if fault == "stale":
        identity = current[0].target.identity.model_copy(update={"generation_id": OTHER})
        monkeypatch.setattr(
            refresh,
            "check_generation_projection",
            lambda *a, **k: transport.GenerationProjectionTarget(binding, identity),
        )
    elif fault == "intent":
        refresh.refresh_paths(binding, USER_ID).intent.unlink()
    elif fault == "origin":
        monkeypatch.setattr(responses, "resolve_api_url", lambda: "https://other.example")

    def forbidden(*a, **k):
        pytest.fail("Unexpected history request or refresh")

    monkeypatch.setattr(refresh, "recover_generation_refresh", forbidden)
    monkeypatch.setattr(refresh, "commit_generation_refresh", forbidden)
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = responses.diff_since_last_session(
            binding,
            True if fault == "days" else None,
            actor=USER_ID,
            transport=transport.HttpHistoryTransport(binding.server_url, client),
        )
    assert result.is_error is True


def test_history_adapter_has_only_dormant_consumers():
    root = Path(dispatch.__file__).parents[1]
    consumers = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.sync.history_transport":
                consumers.add(path.relative_to(root).as_posix())
            if isinstance(node, ast.Import):
                assert all(a.name != "nauro.sync.history_transport" for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.sync":
                assert all(a.name != "history_transport" for a in node.names)
    assert consumers == {"mcp/generation_responses.py", "mcp/read_dispatch.py"}
