from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import ssl
from pathlib import Path

import httpx
import pytest

from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    RefreshRequiredError,
    ReplicaActorMismatchError,
)
from nauro.store.generation_projection import GenerationProjectionVerificationError
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync import generation_acquisition as acquisition
from nauro.sync import transfer
from nauro.sync.generation_acquisition import (
    GenerationAcquisitionError,
    GenerationSupersededError,
    LegacyStoreAuthorityError,
    acquire_generation_projection,
)
from nauro.sync.remote import TransferBoundaryError, TransferSession
from tests.conftest import seed_auth_config

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
OTHER_USER_ID = "01K44444444444444444444444"
SCOPE_ID = "a" * 64
COMMITTED_AT = "2999-12-31T23:59:59.999999Z"
SIDECAR = "questions-provenance.json"
GOLDEN_ARTIFACTS = {"project.md": b"# Project\n"}
GOLDEN_ENVELOPE = (
    b'{"artifacts":{"project.md":'
    b'"aef277fb6a70a89681a85e1b6d23f44ee2a6cc58490f9f5c95fc99db6d2d3542"},'
    b'"committed_at":"2999-12-31T23:59:59.999999Z",'
    b'"generation_id":"01K11111111111111111111111",'
    b'"project_id":"01KQ6AZGNA0B3QBF67NBXP3S45",'
    b'"projection_class":"contributor_plus",'
    b'"projection_scope_id":"' + b"a" * 64 + b'","store_format_version":1}'
)
GOLDEN_DIGEST = "94f488a870f6549bea395828dc077a46023cd8fc36667e4f4c65ce0d471298cd"
THREE_ARTIFACTS = {"state.md": b"# State\n", "project.md": b"# Project\n", SIDECAR: b"{}"}
BINDING = ResolvedProjectBinding(Path("store"), PROJECT_ID, "Nauro", "cloud", "https://api.test")
LOCAL_BINDING = ResolvedProjectBinding(Path("store"), PROJECT_ID, "Nauro", "local", None)
PROJECTION = ("GET", "api.test/generations/projection")
PRESIGN = ("POST", "api.test/generations/presign")
EXCLUDED_MODULES = frozenset({"replica_control", "state", "pull", "hooks"})
LEGACY_409 = {"error": "legacy_store_authority"}


def _json(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode("utf-8"))


def _decisions(count: int) -> dict[str, bytes]:
    return {f"decisions/{number:03d}.md": f"# {number}\n".encode() for number in range(count)}


class _Body(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.pulled = chunks, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


class FakeServer:
    def __init__(self, artifacts=None, *, projection_class: str = "contributor_plus") -> None:
        self.artifacts = dict(GOLDEN_ARTIFACTS if artifacts is None else artifacts)
        self.projection_class = projection_class
        self.generation_id = GENERATION_ID
        self.manifest_committed_at: str | None = COMMITTED_AT
        self.identity_override: dict[str, object] = {}
        self.projection_hook = self.presign_hook = self.object_hook = None
        self.requests: list[tuple[str, str, object, str | None]] = []
        self.counts = {"projection": 0, "presign": 0, "object": 0}
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.session = TransferSession(client=client)

    def envelope(self) -> bytes:
        body = {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": self.generation_id,
            "committed_at": self.manifest_committed_at,
            "projection_class": self.projection_class,
            "projection_scope_id": SCOPE_ID,
            "artifacts": {
                path: hashlib.sha256(content).hexdigest()
                for path, content in self.artifacts.items()
            },
        }
        if self.manifest_committed_at is None:
            body.pop("committed_at")
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return text.encode("utf-8")

    def identity(self) -> dict[str, object]:
        return {
            "project_id": PROJECT_ID,
            "store_format_version": 1,
            "generation_id": self.generation_id,
            "manifest_digest": hashlib.sha256(self.envelope()).hexdigest(),
            "committed_at": COMMITTED_AT,
            "installed_for_user_id": USER_ID,
            "projection_class": self.projection_class,
            "projection_scope_id": SCOPE_ID,
        } | self.identity_override

    def calls(self) -> list[tuple[str, str]]:
        return [(method, route) for method, route, _body, _bearer in self.requests]

    def presign_bodies(self) -> list[dict]:
        return [body for _method, route, body, _bearer in self.requests if route == PRESIGN[1]]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        route = f"{request.url.host}{request.url.path}"
        self.requests.append((request.method, route, body, request.headers.get("Authorization")))
        if request.url.host == "objects.test":
            return self._object(request.url.path)
        if request.url.path == "/oauth/token":
            return _json(200, {"access_token": "tok_new"})
        if request.url.path == "/generations/projection":
            self.counts["projection"] += 1
            carrier = base64.b64encode(self.envelope()).decode("ascii")
            payload = {"projection": self.identity(), "manifest_base64": carrier}
            return self._reply(self.projection_hook, self.counts["projection"], payload)
        assert request.url.path == "/generations/presign"
        self.counts["presign"] += 1
        if body["generation_id"] != self.generation_id:
            return _json(409, {"error": "generation_superseded"})
        mint, base = self.counts["presign"], f"https://objects.test/{self.generation_id}"
        urls = [{"path": path, "url": f"{base}/{path}?mint={mint}"} for path in body["paths"]]
        payload = {"projection": self.identity(), "urls": urls, "expires_at": COMMITTED_AT}
        return self._reply(self.presign_hook, mint, payload)

    def _reply(self, hook, count: int, payload: dict) -> httpx.Response:
        reply = payload if hook is None else hook(count, payload)
        return reply if isinstance(reply, httpx.Response) else _json(200, reply)

    def _object(self, url_path: str) -> httpx.Response:
        self.counts["object"] += 1
        _generation, _slash, path = url_path.lstrip("/").partition("/")
        reply = None if self.object_hook is None else self.object_hook(self.counts["object"], path)
        return httpx.Response(200, content=self.artifacts[path]) if reply is None else reply


def _supersede(server: FakeServer, _payload: dict | None = None) -> httpx.Response:
    server.generation_id = f"01K{server.counts['presign']:023d}"
    return _json(409, {"error": "generation_superseded", "current_generation_id": "advanced"})


def _moved_page(server: FakeServer, payload: dict) -> dict:
    _supersede(server)
    payload["projection"]["generation_id"] = server.generation_id
    return payload


def acquire(server: FakeServer, binding=BINDING, user_id: str | None = USER_ID):
    return acquire_generation_projection(binding, active_user_id=user_id, session=server.session)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_auth_config(variant="sync")
    monkeypatch.setenv("NAURO_API_URL", "https://api.test")
    monkeypatch.setenv("NAURO_AUTH0_DOMAIN", "api.test")
    monkeypatch.setenv("NAURO_AUTH0_CLIENT_ID", "test-client")
    monkeypatch.setattr(transfer, "pause", lambda _seconds: None)


def test_happy_path_orders_projection_presign_then_sorted_gets() -> None:
    server = FakeServer(THREE_ARTIFACTS)
    proof = acquire(server)
    gets = [("GET", f"objects.test/{GENERATION_ID}/{path}") for path in sorted(THREE_ARTIFACTS)]
    assert server.calls() == [PROJECTION, PRESIGN, *gets]
    assert server.presign_bodies() == [
        {"project_id": PROJECT_ID, "generation_id": GENERATION_ID, "paths": sorted(THREE_ARTIFACTS)}
    ]
    assert {artifact.path: artifact.content for artifact in proof.artifacts} == THREE_ARTIFACTS
    assert proof.target.identity.model_dump() == server.identity()
    assert proof.target.binding == BINDING and proof.manifest_json == server.envelope()


def test_golden_envelope_bytes_and_digest_are_pinned() -> None:
    server = FakeServer()
    assert server.envelope() == GOLDEN_ENVELOPE and len(GOLDEN_ENVELOPE) == 379
    assert hashlib.sha256(GOLDEN_ENVELOPE).hexdigest() == GOLDEN_DIGEST
    proof = acquire(server)
    assert proof.manifest_json == GOLDEN_ENVELOPE
    assert proof.target.identity.manifest_digest == GOLDEN_DIGEST


@pytest.mark.parametrize(
    "manifest_committed_at",
    [None, "2998-12-31T23:59:59.999999Z"],
    ids=["current-six-field-server", "divergent-time"],
)
def test_manifest_commit_time_is_bound_before_presign(manifest_committed_at) -> None:
    server = FakeServer()
    server.manifest_committed_at = manifest_committed_at
    if manifest_committed_at is None:
        assert len(server.envelope()) == 334
        assert hashlib.sha256(server.envelope()).hexdigest() == (
            "32c59ab83e93cb28ea902441e86ce4ae125091ab79328d563a5a7f0eab0be967"
        )
    with pytest.raises(GenerationProjectionVerificationError):
        acquire(server)
    assert server.calls() == [PROJECTION]


@pytest.mark.parametrize(
    ("binding", "user_id", "error"),
    [
        (BINDING, None, ReplicaActorMismatchError),
        (BINDING, "user", ReplicaActorMismatchError),
        (LOCAL_BINDING, USER_ID, GenerationAcquisitionError),
    ],
)
def test_bad_binding_or_actor_is_refused_before_any_request(binding, user_id, error) -> None:
    server = FakeServer()
    with pytest.raises(error):
        acquire(server, binding, user_id)
    assert server.calls() == []


def test_echoed_actor_must_equal_the_login_recorded_user() -> None:
    # Without the actor check the run proceeds to presign and the calls assertion fails.
    server = FakeServer()
    server.identity_override = {"installed_for_user_id": OTHER_USER_ID}
    with pytest.raises(ReplicaActorMismatchError, match="issued for another account"):
        acquire(server)
    assert server.calls() == [PROJECTION]


@pytest.mark.parametrize("kind", ["format", "409"])
def test_unsupported_format_raises_client_upgrade_required_before_presign(kind: str) -> None:
    server = FakeServer()
    if kind == "format":
        server.identity_override = {"store_format_version": 2}
    else:
        server.projection_hook = lambda _n, _p: _json(409, {"error": "client_upgrade_required"})
    with pytest.raises(ClientUpgradeRequiredError):
        acquire(server)
    assert server.calls() == [PROJECTION]


def test_legacy_authority_refusal_is_typed_and_leaves_the_origin_open() -> None:
    server = FakeServer()
    server.projection_hook = lambda n, p: _json(409, LEGACY_409) if n == 1 else p
    with pytest.raises(LegacyStoreAuthorityError):
        acquire(server)
    acquire(server)
    assert server.counts["projection"] == 2


def test_wrong_manifest_digest_is_refused_before_presign() -> None:
    # Without the digest check the envelope parses and the raises assertion fails.
    server = FakeServer()
    server.identity_override = {"manifest_digest": "b" * 64}
    with pytest.raises(GenerationProjectionVerificationError, match="digest diverges"):
        acquire(server)
    assert server.calls() == [PROJECTION]


@pytest.mark.parametrize("kind", ["padding", "alphabet", "bom", "duplicate", "nan", "oversize"])
def test_bad_carrier_or_malformed_body_is_refused_before_presign(kind: str, monkeypatch) -> None:
    server = FakeServer()
    if kind == "oversize":
        monkeypatch.setattr(acquisition, "_MAX_PROJECTION_RESPONSE_BYTES", 16)

    def hook(_n: int, payload: dict) -> dict | httpx.Response:
        carrier = payload["manifest_base64"]
        assert carrier.endswith("fQ==")
        if kind == "padding":
            payload["manifest_base64"] = carrier[:-4] + "fR=="
        elif kind == "alphabet":
            payload["manifest_base64"] = carrier[:-4] + "f!=="
        text = json.dumps(payload)
        if kind == "bom":
            return httpx.Response(200, content=b"\xef\xbb\xbf" + text.encode("utf-8"))
        if kind == "duplicate":
            text = text.replace('"manifest_base64":', '"manifest_base64": "x", "manifest_base64":')
        if kind == "nan":
            text = text.replace('"store_format_version": 1', '"store_format_version": NaN')
        return httpx.Response(200, content=text.encode("utf-8"))

    server.projection_hook = hook
    with pytest.raises(GenerationAcquisitionError):
        acquire(server)
    assert server.calls() == [PROJECTION]


def test_viewer_envelope_listing_the_sidecar_is_refused_before_presign() -> None:
    server = FakeServer(THREE_ARTIFACTS, projection_class="viewer")
    with pytest.raises(GenerationProjectionVerificationError, match="Viewer generation"):
        acquire(server)
    assert server.calls() == [PROJECTION]


@pytest.mark.parametrize("kind", ["extra", "missing", "unrequested", "http"])
def test_presign_reply_must_cover_exactly_the_requested_paths_over_https(kind: str) -> None:
    server = FakeServer(THREE_ARTIFACTS)

    def hook(_n: int, payload: dict) -> dict:
        urls = payload["urls"]
        if kind == "extra":
            urls.append(dict(urls[0]))
        elif kind == "missing":
            urls.pop()
        elif kind == "unrequested":
            urls[0]["path"] = "stack.md"
        else:
            urls[0]["url"] = "http://" + urls[0]["url"].removeprefix("https://")
        return payload

    server.presign_hook = hook
    with pytest.raises(GenerationAcquisitionError):
        acquire(server)
    assert server.counts["object"] == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("projection_scope_id", "b" * 64, RefreshRequiredError),
        ("projection_class", "viewer", RefreshRequiredError),
        ("installed_for_user_id", OTHER_USER_ID, ReplicaActorMismatchError),
    ],
)
def test_moved_fact_on_page_two_is_refused_without_page_two_gets(field, value, error) -> None:
    # Without the actor branch the actor row raises RefreshRequiredError; without the eight-fact
    # comparison page two downloads. Either way the raises assertion fails.
    server = FakeServer(_decisions(201))

    def hook(n: int, payload: dict) -> dict:
        if n == 2:
            payload["projection"][field] = value
        return payload

    server.presign_hook = hook
    with pytest.raises(error):
        acquire(server)
    assert (server.counts["presign"], server.counts["object"]) == (2, 200)


@pytest.mark.parametrize("advance", [_supersede, _moved_page], ids=["409", "200"])
def test_generation_advance_on_presign_restarts_once_and_completes(advance) -> None:
    # Without the generation branch the 200 row raises RefreshRequiredError at acquire.
    server = FakeServer(THREE_ARTIFACTS)
    server.presign_hook = lambda n, p: advance(server, p) if n == 1 else p
    proof = acquire(server)
    assert server.counts["projection"] == 2
    assert proof.target.identity.generation_id == server.generation_id != GENERATION_ID
    assert proof.manifest_json == server.envelope()


def test_generation_advancing_on_every_pass_raises_after_three_projection_calls() -> None:
    server = FakeServer(THREE_ARTIFACTS)
    server.presign_hook = lambda _n, _p: _supersede(server)
    with pytest.raises(GenerationSupersededError):
        acquire(server)
    assert (server.counts["projection"], server.counts["object"]) == (3, 0)


def test_expired_get_remints_only_the_outstanding_paths_of_the_page() -> None:
    server = FakeServer(THREE_ARTIFACTS)
    server.object_hook = lambda n, path: httpx.Response(403) if (n, path) == (2, SIDECAR) else None
    proof = acquire(server)
    bodies = server.presign_bodies()
    assert [body["paths"] for body in bodies] == [sorted(THREE_ARTIFACTS), [SIDECAR, "state.md"]]
    assert {body["generation_id"] for body in bodies} == {GENERATION_ID}
    assert server.counts["object"] == 4 and len(proof.artifacts) == 3


def test_second_expiry_on_the_same_path_is_permanent() -> None:
    server = FakeServer(THREE_ARTIFACTS)
    server.object_hook = lambda _n, path: httpx.Response(403) if path == SIDECAR else None
    with pytest.raises(TransferBoundaryError) as caught:
        acquire(server)
    assert caught.value.status == 403 and server.counts["presign"] == 2


def test_remint_that_returns_a_moved_generation_restarts() -> None:
    server = FakeServer(THREE_ARTIFACTS)
    server.object_hook = lambda n, _path: httpx.Response(403) if n == 1 else None
    server.presign_hook = lambda n, p: _supersede(server) if n == 2 else p
    proof = acquire(server)
    assert server.counts["projection"] == 2
    assert proof.target.identity.generation_id == server.generation_id != GENERATION_ID


@pytest.mark.parametrize(("failures", "succeeds"), [(1, True), (4, False)])
def test_transient_get_faults_retry_within_the_budget(failures: int, succeeds: bool) -> None:
    server = FakeServer()
    server.object_hook = lambda n, _path: httpx.Response(503) if n <= failures else None
    if succeeds:
        assert acquire(server).artifacts[0].content == b"# Project\n"
    else:
        with pytest.raises(TransferBoundaryError) as caught:
            acquire(server)
        assert caught.value.status == 503
    assert server.counts["object"] == failures + int(succeeds)


def test_declared_oversize_artifact_is_refused_before_the_body_is_read() -> None:
    # Without the Content-Length cap the body streams and the pulled assertion fails.
    server = FakeServer()
    body = _Body([b"x"])
    declared = {"Content-Length": str(4 * 1024 * 1024 + 1)}
    server.object_hook = lambda _n, _path: httpx.Response(200, headers=declared, stream=body)
    with pytest.raises(GenerationAcquisitionError, match="size cap: project.md"):
        acquire(server)
    assert (body.pulled, server.counts["object"]) == (0, 1)


def test_streamed_oversize_artifact_is_refused_mid_stream() -> None:
    # Without the running-total cap all six chunks stream and the pulled assertion fails.
    server = FakeServer()
    body = _Body([b"x" * (1024 * 1024)] * 6)
    server.object_hook = lambda _n, _path: httpx.Response(200, stream=body)
    with pytest.raises(GenerationAcquisitionError, match="size cap: project.md"):
        acquire(server)
    assert (body.pulled, server.counts["object"]) == (5, 1)


def test_total_cap_refuses_before_the_second_body_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the total cap all three downloads complete and the raises assertion fails.
    monkeypatch.setattr(acquisition, "_MAX_PROJECTION_BYTES", 11)
    server = FakeServer(THREE_ARTIFACTS)
    with pytest.raises(GenerationAcquisitionError, match="total size cap"):
        acquire(server)
    assert server.counts["object"] == 2


def test_certificate_failure_trips_the_object_origin_for_the_session() -> None:
    server = FakeServer()

    def hook(n: int, _path: str) -> None:
        if n == 1:
            raise httpx.ConnectError("connect failed") from ssl.SSLCertVerificationError("cert")

    server.object_hook = hook
    with pytest.raises(TransferBoundaryError) as first:
        acquire(server)
    with pytest.raises(TransferBoundaryError) as second:
        acquire(server)
    assert first.value.kind.value == "tls-certificate"
    assert second.value.kind.value == "origin-aborted"
    assert (server.counts["object"], server.counts["projection"]) == (1, 2)


def test_first_401_refreshes_the_token_and_retries_with_the_new_bearer() -> None:
    server = FakeServer()
    server.projection_hook = lambda n, p: httpx.Response(401) if n == 1 else p
    acquire(server)
    assert server.calls()[:3] == [PROJECTION, ("POST", "api.test/oauth/token"), PROJECTION]
    bearers = [bearer for _m, route, _b, bearer in server.requests if route == PROJECTION[1]]
    assert bearers == ["Bearer tok_orig", "Bearer tok_new"]
    assert not BINDING.store_path.exists()


def test_acquisition_touches_no_disk_control_state_or_sync_state(monkeypatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("generation acquisition must stay in memory")

    server = FakeServer(THREE_ARTIFACTS)
    monkeypatch.setattr("nauro.store.replica_control.locked_replica_control_snapshot", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr("nauro.sync.state.save_state", forbidden)
    assert len(acquire(server).artifacts) == 3
    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(acquisition.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            if node.module in ("nauro.sync", "nauro.store"):
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not {name for name in imported if name.rsplit(".", 1)[-1] in EXCLUDED_MODULES}
