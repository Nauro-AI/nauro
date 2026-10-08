"""One projection transfer per generation operation, never under a replica lock."""

from __future__ import annotations

import base64
import json
import threading
from contextlib import ExitStack, suppress
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from nauro.auth import ActiveCredentials
from nauro.cli.main import app
from nauro.mcp import generation_responses as responses
from nauro.mcp import stdio_server
from nauro.mcp.payloads import build_guidance_payload
from nauro.store.generation_authority import RefreshRequiredError, ReplicaActorMismatchError
from nauro.store.generation_projection import (
    GenerationProjectionTarget,
    GenerationProjectionVerificationError,
    verify_generation_projection,
)
from nauro.store.generation_refresh_io import refresh_paths
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.home import nauro_home
from nauro.store.replica_control import ReplicaControlBusyError, _native_control_lock
from nauro.sync import generation_acquisition as acquisition
from nauro.sync import generation_decision as delivery
from nauro.sync import generation_guidance as guidance
from nauro.sync import generation_refresh_status as refresh_status
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.history_transport import HttpHistoryTransport
from nauro.templates.generation_guidance import regenerate_refreshed_guidance
from tests import test_generation_installation as fixtures
from tests.test_generation_refresh_contention import _held_elsewhere
from tests.test_history_transport import response_body
from tests.test_stdio_startup_authority import installed as installed_fixture

PROJECTION = "/generations/projection"
ADVANCED = [PROJECTION, "/generations/presign", "/state.md"]
OTHER = "01K88888888888888888888888"
_PROBES = threading.Lock()


class Hosted:
    """Serves one projection and records every request, probing both locks from another thread."""

    def __init__(self, binding, connection):
        self.projection = fixtures._projection({"state.md": b"Committed generation state\n"})
        self.status = 200
        self.paths: list[str] = []
        self.locked: list[str] = []
        store = refresh_paths(binding, fixtures.USER_ID).store
        attempt = refresh_status._attempt_path(binding, connection, fixtures.USER_ID)
        self.locks = (
            (store, store / ".replica-control.lock"),
            (nauro_home(), attempt.with_suffix(".lock")),
        )

    def send(self, request: httpx.Request) -> httpx.Response:
        self._probe(request.url.path)
        self.paths.append(request.url.path)
        identity = self.projection.target.identity.model_dump()
        if request.url.path == PROJECTION:
            manifest = base64.b64encode(self.projection.manifest_json).decode()
            body = {"projection": identity, "manifest_base64": manifest}
            return httpx.Response(self.status, json=body, request=request)
        if request.url.path == "/generations/presign":
            urls = [
                {"path": path, "url": f"https://objects.example/{path}"}
                for path in json.loads(request.content)["paths"]
            ]
            body = {"projection": identity, "urls": urls, "expires_at": "2999-12-31T23:59:59Z"}
            return httpx.Response(200, json=body, request=request)
        if request.url.path == "/generations/history":
            return httpx.Response(200, json=response_body(self.projection.target), request=request)
        content = self.projection.artifacts_by_path[request.url.path[1:]].content
        return httpx.Response(200, content=content, request=request)

    def _probe(self, path: str) -> None:
        def probe() -> None:
            for root, lock in self.locks:
                try:
                    with _native_control_lock(root, lock, 0):
                        pass
                except ReplicaControlBusyError:
                    self.locked.append(f"{path} under {lock.name}")

        with _PROBES:
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(5)


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    repo, binding, connection, *_ = installed_fixture.__wrapped__(tmp_path, monkeypatch)
    server = Hosted(binding, connection)
    monkeypatch.setattr(
        httpx._client.Client, "send", lambda self, request, **kw: server.send(request)
    )
    return repo, binding, server


def _advance(server, monkeypatch):
    monkeypatch.setattr(fixtures, "GENERATION_ID", "01K77777777777777777777777")
    server.projection = fixtures._projection({"state.md": b"New committed state\n"})


def _rebuilt(projection, binding, **identity):
    target = GenerationProjectionTarget(
        binding, projection.target.identity.model_copy(update=identity)
    )
    artifacts = tuple((a.path, a.content) for a in projection.artifacts)
    return verify_generation_projection(
        target, manifest_json=projection.manifest_json, artifacts=artifacts
    )


def _receipt(binding, monkeypatch):
    committed = {"status": "committed", "execution": {"receipt_json": "{}"}}
    monkeypatch.setattr(
        delivery.DecisionReferenceTransport, "propose_decision", Mock(return_value=committed)
    )
    selected = (refresh_status._account(binding)[0], binding.project_id)
    result = delivery.execute_decision(
        selected, {"request_mode": "submit"}, on_refreshed=regenerate_refreshed_guidance
    )
    assert result["guidance_status"]["status"] == "completed"


def _history(binding):
    client = httpx._client.Client(trust_env=False)
    adapter = HttpHistoryTransport(
        binding.server_url,
        client,
        credentials=lambda: ActiveCredentials(fixtures.USER_ID, "generation-token"),
    )
    with GenerationTransferSession(binding) as session:
        result = responses.diff_since_last_session(
            binding, actor=fixtures.USER_ID, transport=adapter, session=session
        )
    assert result.is_error is False, result.text


def _sync(binding):
    result = CliRunner().invoke(app, ["sync", "--project", binding.project_id])
    assert result.exit_code == 0, result.output


READS = {
    "get_context": lambda pid: stdio_server.get_context(project_id=pid),
    "get_decision": lambda pid: stdio_server.get_decision(1, project_id=pid),
    "get_raw_file": lambda pid: stdio_server.get_raw_file("state.md", project_id=pid),
    "list_decisions": lambda pid: stdio_server.list_decisions(project_id=pid),
    "search_decisions": lambda pid: stdio_server.search_decisions("state", project_id=pid),
    "check_decision": lambda pid: stdio_server.check_decision("Keep state", project_id=pid),
}
OPERATIONS = {
    "startup": lambda binding, _: stdio_server._pull_on_startup(),
    "cli_refresh": lambda binding, _: _sync(binding),
    "post_commit": _receipt,
    "guidance": lambda binding, _: build_guidance_payload(binding.store_path),
    "history": lambda binding, _: _history(binding),
}
REFRESHES = ("startup", "cli_refresh", "post_commit")


@pytest.mark.parametrize(
    "operation,advanced,expected",
    [
        ("startup", False, [PROJECTION]),
        ("startup", True, ADVANCED),
        ("cli_refresh", False, [PROJECTION]),
        ("cli_refresh", True, ADVANCED),
        ("post_commit", False, [PROJECTION]),
        ("post_commit", True, ADVANCED),
        ("guidance", False, [PROJECTION]),
        ("history", False, ["/generations/history", PROJECTION]),
        *[(name, False, [PROJECTION]) for name in READS],
    ],
)
def test_one_transfer_and_no_network_under_either_lock(
    hosted, monkeypatch, operation, advanced, expected
):
    repo, binding, server = hosted
    if advanced:
        _advance(server, monkeypatch)
    if operation in READS:
        READS[operation](binding.project_id)
    else:
        OPERATIONS[operation](binding, monkeypatch)
    assert server.paths == expected
    assert server.locked == []
    if operation in REFRESHES:
        assert refresh_status.replica_status(binding)["last_refresh_error_code"] is None
        identity = server.projection.target.identity
        assert identity.generation_id in (repo / "AGENTS.md").read_text()


def test_revoked_read_refuses_without_content(hosted):
    _, binding, server = hosted
    server.status = 403
    result = stdio_server.get_raw_file("state.md", project_id=binding.project_id)
    assert result.isError is True
    assert "Committed generation state" not in repr(result)
    assert server.paths == [PROJECTION]


@pytest.mark.parametrize("change", ["target", "actor", "binding"])
def test_refresh_guidance_confirms_the_installed_snapshot(hosted, monkeypatch, change):
    _, binding, server = hosted
    snapshot = refresh_status.refresh_replica(binding)
    server.paths.clear()
    rendered = guidance.read_generation_guidance(binding.store_path, str, snapshot=snapshot)
    assert rendered is not None and rendered[1] == snapshot.target.identity
    if change == "target":
        _advance(server, monkeypatch)
        projection = _rebuilt(server.projection, binding)
    elif change == "actor":
        projection = _rebuilt(server.projection, binding, installed_for_user_id=OTHER)
    else:
        projection = _rebuilt(server.projection, replace(binding, display_name="Other"))
    with pytest.raises(PermissionError, match="Generation guidance unavailable"):
        guidance.read_generation_guidance(
            binding.store_path,
            lambda store: "MUST NOT ESCAPE",
            snapshot=GenerationSnapshotStore(projection),
        )
    assert server.paths == []


@pytest.mark.parametrize(
    "change,expected",
    [
        ("binding", acquisition.GenerationAcquisitionError),
        ("actor", ReplicaActorMismatchError),
        ("manifest", GenerationProjectionVerificationError),
        ("session", GenerationProjectionVerificationError),
    ],
)
def test_acquisition_refuses_a_foreign_observation(hosted, change, expected):
    _, binding, server = hosted
    target = GenerationProjectionTarget(binding, server.projection.target.identity)
    manifest = server.projection.manifest_json
    if change == "binding":
        target = replace(target, binding=replace(binding, display_name="Other"))
    elif change == "actor":
        identity = target.identity.model_copy(update={"installed_for_user_id": OTHER})
        target = GenerationProjectionTarget(binding, identity)
    elif change == "manifest":
        manifest = manifest.replace(b"state.md", b"other.md")
    with GenerationTransferSession(binding) as session, GenerationTransferSession(binding) as other:
        owner = other if change == "session" else session
        observed = acquisition.ObservedGenerationProjection(target, manifest, owner)
        with pytest.raises(expected):
            acquisition.acquire_generation_projection(
                binding, active_user_id=fixtures.USER_ID, session=session, observed=observed
            )
    assert server.paths == []


def test_observation_is_reusable_only_under_a_caller_session(hosted):
    _, binding, server = hosted
    with pytest.raises(acquisition.GenerationAcquisitionError):
        acquisition.observe_generation_projection(
            binding, active_user_id=fixtures.USER_ID, session=None
        )
    assert server.paths == []
    with GenerationTransferSession(binding) as session:
        observed = acquisition.observe_generation_projection(
            binding, active_user_id=fixtures.USER_ID, session=session
        )
        acquired = acquisition.acquire_generation_projection(
            binding, active_user_id=fixtures.USER_ID, session=session, observed=observed
        )
    assert acquired.target == observed.target
    assert server.paths == ADVANCED


@pytest.mark.parametrize("later", ["success", "incomplete"])
@pytest.mark.parametrize("clock", ["forward", "backward"])
def test_failed_refresh_keeps_a_newer_success(hosted, monkeypatch, later, clock):
    _, binding, server = hosted
    prepare = refresh_status.prepare_generation_refresh
    if clock == "backward":
        stamps = iter(f"2026-09-01T00:0{minute}:00.000000Z" for minute in (5, 1, 2))
        monkeypatch.setattr(refresh_status, "_now", lambda: next(stamps))
    recorded = []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    def superseded(*args, **kwargs):
        newer = prepare if later == "success" else interrupted
        monkeypatch.setattr(refresh_status, "prepare_generation_refresh", newer)
        with suppress(KeyboardInterrupt):
            refresh_status.refresh_replica(binding)
        recorded.append(refresh_status.replica_status(binding))
        raise RefreshRequiredError("superseded")

    monkeypatch.setattr(refresh_status, "prepare_generation_refresh", superseded)
    with pytest.raises(RefreshRequiredError, match="superseded"):
        refresh_status.refresh_replica(binding)
    status = refresh_status.replica_status(binding)
    assert status == recorded[0]
    assert status["last_refresh_error_code"] == (
        None if later == "success" else "refresh_incomplete"
    )
    assert server.locked == []


def test_busy_failure_record_keeps_the_original_error(hosted, monkeypatch):
    _, binding, _ = hosted
    connection = refresh_status._account(binding)[0]
    lock = refresh_status._attempt_path(binding, connection, fixtures.USER_ID).with_suffix(".lock")
    held = ExitStack()

    def failed(*args, **kwargs):
        held.enter_context(_held_elsewhere(lock))
        raise RefreshRequiredError("original")

    monkeypatch.setattr(refresh_status, "prepare_generation_refresh", failed)
    with held, pytest.raises(RefreshRequiredError, match="original"):
        refresh_status.refresh_replica(binding)
    assert refresh_status.replica_status(binding)["last_refresh_error_code"] == "refresh_incomplete"
