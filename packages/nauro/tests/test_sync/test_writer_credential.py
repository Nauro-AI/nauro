import json
from unittest.mock import patch

import httpx
import pytest

from nauro.sync.remote import PresignError, request_presigned_urls
from nauro.sync.writer_credential import writer_headers
from tests.conftest import seed_auth_config

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
API = "https://mcp.nauro.ai"
TOKEN = "a" * 64


@pytest.fixture
def credential(tmp_path, monkeypatch):
    path = tmp_path / "writer.json"
    path.write_text(json.dumps({"project_id": PROJECT, "api_url": API, "token": TOKEN}))
    path.chmod(0o600)
    monkeypatch.setenv("NAURO_WRITER_CREDENTIAL_FILE", str(path))
    return path


def test_project_and_origin_binding(credential):
    assert writer_headers(PROJECT, API) == {"X-Nauro-Writer-Token": TOKEN}
    assert writer_headers("another-project", API) == {}
    with pytest.raises(PresignError, match="Writer credential is unavailable or invalid"):
        writer_headers(PROJECT, "https://other.example")


@pytest.mark.parametrize("failure", ["missing", "permissions", "malformed"])
def test_invalid_credential_does_not_reach_http(credential, failure):
    if failure == "missing":
        credential.unlink()
    elif failure == "permissions":
        credential.chmod(0o644)
    else:
        credential.write_text(TOKEN)
    with patch("nauro.sync.remote.httpx.Client.post") as post:
        with pytest.raises(PresignError) as error:
            request_presigned_urls(PROJECT, [{"verb": "PUT", "path": "project.md"}])
    post.assert_not_called()
    assert str(error.value) == "Writer credential is unavailable or invalid."


@pytest.mark.parametrize("verb", ["PUT", "GET"])
def test_header_only_on_upload_presign_and_auth_retry(credential, verb):
    seed_auth_config(variant="sync")
    attempts = []

    def refresh(call, **kwargs):
        call("old-access-token")
        return call("new-access-token")

    def post(url, **kwargs):
        attempts.append(kwargs["headers"])
        return httpx.Response(200, json={"urls": []})

    with (
        patch("nauro.sync.remote.resolve_api_url", return_value=API),
        patch("nauro.sync.remote.with_token_refresh", side_effect=refresh),
        patch("nauro.sync.remote.httpx.Client.post", side_effect=post),
    ):
        assert request_presigned_urls(PROJECT, [{"verb": verb, "path": "project.md"}]) == []
    extra = {"X-Nauro-Writer-Token": TOKEN} if verb == "PUT" else {}
    assert attempts == [
        {"Authorization": "Bearer old-access-token", **extra},
        {"Authorization": "Bearer new-access-token", **extra},
    ]


def test_unconfigured_client_keeps_legacy_transport(monkeypatch):
    monkeypatch.delenv("NAURO_WRITER_CREDENTIAL_FILE", raising=False)
    assert writer_headers(PROJECT, API) == {}
