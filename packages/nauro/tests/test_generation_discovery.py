"""Server authority discovery reads generation only from a positive owner answer."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from nauro import auth
from nauro.store.config import save_config
from nauro.store.home import config_file
from nauro.sync import generation_discovery
from nauro.sync.generation_discovery import AuthorityDiscovery, discover_project_authority
from tests.conftest import seed_auth_config

PID = "01KQ6AZGNA0B3QBF67NBXP3S45"
OTHER = "01KQ6AZGNA0B3QBF67NBXP3S46"


def _row(project_id=PID, role="owner"):
    return {"project_id": project_id, "name": "team", "role": role, "created_at": "2026-01-01"}


def _owner(*rows):
    return {"projects": list(rows) or [_row()], "authority": "generation_owner"}


def _wire(monkeypatch, *responses):
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        status, body = responses[min(len(calls), len(responses)) - 1]
        if isinstance(body, Exception):
            raise body
        request = httpx.Request(
            method, url, params=kwargs.get("params"), headers=kwargs.get("headers")
        )
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(generation_discovery.httpx, "request", handler)
    return calls


@pytest.fixture
def legacy_token(monkeypatch):
    seed_auth_config(access_token="legacy-token")
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, _owner(), ("generation", 200, None)),
        (200, _owner(_row(OTHER), _row()), ("generation", 200, None)),
        (
            409,
            {"detail": "generation_authority_required"},
            ("legacy", 409, "generation_authority_required"),
        ),
        (409, {"detail": "single_writer_refused"}, ("legacy", 409, "single_writer_refused")),
        (503, {"detail": "authority_unavailable"}, ("legacy", 503, "authority_unavailable")),
        (401, {"detail": "invalid_token"}, ("legacy", None, None)),
        (403, {"detail": "forbidden"}, ("legacy", 403, "forbidden")),
        (403, {"detail": "insufficient_scope"}, ("legacy", 403, "insufficient_scope")),
        (404, {"detail": "Not Found"}, ("legacy", 404, "Not Found")),
        (400, {"error": "invalid_project"}, ("legacy", 400, None)),
        (500, {"error": "internal"}, ("legacy", 500, None)),
        (502, "Bad Gateway", ("legacy", 502, None)),
        (200, "not json", ("legacy", 200, None)),
        (200, [_row()], ("legacy", 200, None)),
        (200, {"projects": [_row()]}, ("legacy", 200, None)),
        (200, _owner(_row(role="member")), ("legacy", 200, None)),
        (200, _owner(_row(role="viewer")), ("legacy", 200, None)),
        (200, _owner(_row(OTHER)), ("legacy", 200, None)),
        (200, _owner({**_row(OTHER), "id": PID}), ("legacy", 200, None)),
        (200, _owner(_row(role="Owner")), ("legacy", 200, None)),
        (200, {"projects": [_row()], "authority": "generation"}, ("legacy", 200, None)),
        (200, {"projects": _row(), "authority": "generation_owner"}, ("legacy", 200, None)),
        (200, {"projects": [], "authority": "generation_owner"}, ("legacy", 200, None)),
        (200, {"projects": [PID], "authority": "generation_owner"}, ("legacy", 200, None)),
        (201, _owner(), ("legacy", 201, None)),
        (503, _owner(), ("legacy", 503, None)),
    ],
)
def test_only_a_positive_owner_answer_reads_as_generation(
    legacy_token, monkeypatch, status, body, expected
):
    _wire(monkeypatch, (status, body))
    assert discover_project_authority(PID) == AuthorityDiscovery(*expected)


def test_request_is_one_get_with_the_legacy_token(legacy_token, monkeypatch):
    calls = _wire(monkeypatch, (200, _owner()))
    discover_project_authority(PID)
    [(method, url, kwargs)] = calls
    sent = httpx.Request(method, url, params=kwargs["params"])
    assert (method, sent.url.host, sent.url.path) == ("GET", "example.test", "/projects")
    assert list(sent.url.params.multi_items()) == [("project_id", PID)]
    assert kwargs["headers"]["Authorization"] == "Bearer legacy-token"
    assert kwargs["timeout"] == 15.0


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")]
)
def test_transport_errors_and_timeouts_read_as_legacy(legacy_token, monkeypatch, error):
    _wire(monkeypatch, (0, error))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)


def test_no_legacy_token_reads_as_legacy_without_a_request(monkeypatch):
    calls = _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)
    assert calls == []


def test_second_401_after_refresh_reads_as_legacy(legacy_token, monkeypatch):
    monkeypatch.setattr(auth, "refresh_access_token", lambda **_: "fresh-token")
    calls = _wire(monkeypatch, (401, {"detail": "invalid_token"}))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", 401, "invalid_token")
    assert [c[2]["headers"]["Authorization"] for c in calls] == [
        "Bearer legacy-token",
        "Bearer fresh-token",
    ]


@pytest.mark.parametrize(
    "status,body", [(200, _owner()), (409, {"detail": "single_writer_refused"})]
)
def test_discovery_creates_nothing_under_nauro_home(
    legacy_token, monkeypatch, tmp_path, status, body
):
    _wire(monkeypatch, (status, body))
    before = _tree(tmp_path)
    discover_project_authority(PID)
    assert _tree(tmp_path) == before


def test_repository_config_does_not_choose_the_origin(monkeypatch, tmp_path):
    monkeypatch.delenv("NAURO_API_URL", raising=False)
    save_config({"auth": {"access_token": "legacy-token"}, "api_url": "https://global.test"})
    repo = tmp_path / "repo"
    (repo / ".nauro").mkdir(parents=True)
    config = {"id": PID, "mode": "cloud", "server_url": "https://repo.test"}
    (repo / ".nauro" / "config.json").write_text(json.dumps(config))
    monkeypatch.chdir(repo)
    calls = _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID).authority == "generation"
    assert [c[1] for c in calls] == ["https://global.test/projects"]


def test_non_utf8_global_config_reads_as_legacy(monkeypatch):
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    config_file().write_bytes(b'{"auth": {"access_token": "\xff\xfe"}}')
    _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)


def test_deeply_nested_global_config_reads_as_legacy_without_a_request(monkeypatch):
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    config_file().write_text("[" * 100000)
    calls = _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)
    assert calls == []


def test_unreadable_global_config_reads_as_legacy_without_a_request(monkeypatch):
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    config_file().mkdir()
    calls = _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)
    assert calls == []


def test_invalid_origin_url_reads_as_legacy(legacy_token, monkeypatch):
    monkeypatch.setenv("NAURO_API_URL", "http://[::1")
    _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)


def test_non_ascii_legacy_token_reads_as_legacy(monkeypatch):
    seed_auth_config(access_token="t\u00f8ken")
    monkeypatch.setenv("NAURO_API_URL", "https://example.test")
    _wire(monkeypatch, (200, _owner()))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", None, None)


def test_deeply_nested_body_reads_as_legacy(legacy_token, monkeypatch):
    _wire(monkeypatch, (200, "[" * 100000))
    assert discover_project_authority(PID) == AuthorityDiscovery("legacy", 200, None)


def test_non_string_project_id_is_a_programming_error():
    with pytest.raises(TypeError):
        discover_project_authority(None)  # type: ignore[arg-type]


def test_module_never_loads_the_generation_credential_store():
    code = (
        "import sys, nauro.sync.generation_discovery\n"
        "print(sorted(m for m in sys.modules if m.startswith('nauro') and 'credential' in m))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
