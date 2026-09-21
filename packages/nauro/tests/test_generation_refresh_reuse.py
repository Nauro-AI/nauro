from __future__ import annotations

import base64
import json

import httpx
import pytest

from nauro.mcp import stdio_server
from nauro.store.generation_refresh_io import refresh_paths
from nauro.sync import generation_refresh as refresh
from nauro.sync.generation_refresh_status import refresh_replica
from tests import test_generation_installation as fixtures
from tests.test_stdio_startup_authority import installed as installed


@pytest.mark.parametrize("interrupted", [False, True])
def test_installed_refresh_reuses_bytes_and_completes_after_interruption(
    installed, monkeypatch, interrupted
):
    _, binding, _, _, _, _, legacy = installed
    monkeypatch.setattr(fixtures, "GENERATION_ID", "01K77777777777777777777777")
    artifacts = {
        "state.md": b"Committed generation state\n",
        "decisions/002.md": b"# New decision\n",
    }
    projection = fixtures._projection(artifacts)
    with httpx.Client(trust_env=False) as client:
        concrete = type(client)
    downloads, presigns = [], []

    def wire(request):
        assert request.headers.get("Authorization") == (
            None if request.url.host == "objects.example" else "Bearer generation-token"
        )
        if request.url.path == "/generations/projection":
            return httpx.Response(
                200,
                json={
                    "projection": projection.target.identity.model_dump(),
                    "manifest_base64": base64.b64encode(projection.manifest_json).decode(),
                },
            )
        if request.url.path == "/generations/presign":
            paths = json.loads(request.content)["paths"]
            presigns.append(paths)
            return httpx.Response(
                200,
                json={
                    "projection": projection.target.identity.model_dump(),
                    "urls": [{"path": p, "url": f"https://objects.example/{p}"} for p in paths],
                    "expires_at": "2999-12-31T23:59:59Z",
                },
            )
        assert request.url.host == "objects.example"
        path = request.url.path.lstrip("/")
        downloads.append(path)
        return httpx.Response(200, content=artifacts[path])

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: concrete(transport=httpx.MockTransport(wire), **kw)
    )
    original = refresh.sync_file
    if interrupted:
        pointer = refresh_paths(binding, fixtures.USER_ID).pointer

        def fail(paths, path):
            if path == pointer:
                raise OSError("final barrier failed")
            original(paths, path)

        monkeypatch.setattr(refresh, "sync_file", fail)
        with pytest.raises(refresh.GenerationRefreshDurabilityError):
            refresh_replica(binding)
        monkeypatch.setattr(refresh, "sync_file", original)
    store = refresh_replica(binding)
    assert store.read_file("state.md") == artifacts["state.md"].decode()
    assert store.read_file("decisions/002.md") == artifacts["decisions/002.md"].decode()
    attempts = 2 if interrupted else 1
    assert downloads == ["decisions/002.md"] * attempts
    assert presigns == [["decisions/002.md"]] * attempts
    result = stdio_server.get_raw_file("decisions/002.md", project_id=binding.project_id)
    assert result.isError is False
    assert "# New decision" in result.content[0].text
    assert legacy == []
