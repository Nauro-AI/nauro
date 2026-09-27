import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_server import app as routes
from mcp_server.project_store_authority import GENERATION_AUTHORITY_EPOCH_ATTRIBUTE
from nauro.store.config import save_config
from nauro.sync import generation_discovery
from nauro.sync.generation_discovery import AuthorityDiscovery, discover_project_authority
from tests.conftest import TEST_PROJECT_ID
from tests.test_judgment_planning import _table
from tests.test_judgment_transport import signed_transport

__all__ = ["signed_transport"]

KEY = {"pk": f"PROJECT#{TEST_PROJECT_ID}", "sk": "METADATA"}


def _legacy():
    _table().put_item(Item=KEY)


def _single_writer():
    _table().put_item(
        Item={
            "pk": KEY["pk"],
            "sk": "SINGLE_WRITER",
            "mode": "paused",
            "revision": "a" * 32,
            "token_sha256": "",
        }
    )


def _malformed_epoch():
    _table().update_item(
        Key=KEY,
        UpdateExpression="SET #epoch = :epoch",
        ExpressionAttributeNames={"#epoch": GENERATION_AUTHORITY_EPOCH_ATTRIBUTE},
        ExpressionAttributeValues={":epoch": 2},
    )


@pytest.mark.parametrize(
    "seed,expected",
    [
        (None, AuthorityDiscovery("generation", 200, None)),
        (_legacy, AuthorityDiscovery("legacy", 409, "generation_authority_required")),
        (_single_writer, AuthorityDiscovery("legacy", 409, "single_writer_refused")),
        (_malformed_epoch, AuthorityDiscovery("legacy", 503, "authority_unavailable")),
    ],
)
def test_discovery_reads_the_pinned_owner_route(
    signed_transport, tmp_path, monkeypatch, seed, expected
):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    monkeypatch.setenv("NAURO_API_URL", "https://discovery.test")
    save_config({"auth": {"access_token": signed_transport.token}})
    if seed is not None:
        seed()
    app = FastAPI()
    app.add_api_route("/projects", routes.projects_list, methods=["GET"])
    sent = []
    with TestClient(app) as server:

        def wire(method, url, **kwargs):
            sent.append((method, url, kwargs["params"]))
            return server.request(method, url, params=kwargs["params"], headers=kwargs["headers"])

        monkeypatch.setattr(generation_discovery.httpx, "request", wire)
        assert discover_project_authority(TEST_PROJECT_ID) == expected
    assert sent == [("GET", "https://discovery.test/projects", {"project_id": TEST_PROJECT_ID})]
