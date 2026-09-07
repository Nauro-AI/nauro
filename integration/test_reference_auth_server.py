import asyncio
import json
import time

import httpx
import jwt
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp_server import decision_delivery as delivery
from mcp_server.generations import read_generation_pointer
from nauro.cli import decision_reference as cli
from nauro.cli.main import app
from nauro.mcp.decision_reference import reference_server
from nauro.sync.decision_profile import RenewalProfile, profile_transport
from nauro.sync.reference_auth import ReferenceAuth
from tests.conftest import TEST_PROJECT_ID
from tests.test_decision_reference import ORIGIN, probe, signed_transport
from tests.test_judgment_planning import USER_ID, _table
from typer.testing import CliRunner

__all__ = ["probe", "signed_transport"]


@pytest.mark.parametrize("revoke", [False, True])
def test_renewed_credentials_recover_lost_commit_without_resubmission(
    probe,
    signed_transport,
    tmp_path,
    monkeypatch,
    revoke,
):
    tmp_path.chmod(0o700)
    profile = RenewalProfile(
        version=2,
        endpoint=ORIGIN + "/mcp",
        project_id=TEST_PROJECT_ID,
        actor_id=USER_ID,
        credentials_file=str(tmp_path / "credentials.json"),
        issuer="https://test.auth0.com/",
        client_id="synthetic-installed-client",
        audience=ORIGIN + "/mcp",
        expected_subject="transport-user",
        redirect_uri="http://127.0.0.1:18765/callback",
    )
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    path.chmod(0o600)
    old_token = probe.headers["Authorization"][7:]
    new_token = jwt.encode(
        {
            **signed_transport.claims,
            "aud": profile.audience,
            "iat": int(time.time()),
            "azp": profile.client_id,
            "jti": "renewed",
        },
        signed_transport.key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signed_transport.key.public_key(), as_dict=True)
    jwk["kid"] = "test"
    modes, exchanges, committed = [], [], []
    state = {"expired": False}

    def wire(request):
        if request.url.path == "/oauth/token":
            exchanges.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "access_token": new_token,
                    "refresh_token": "next-refresh",
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if state["expired"] and request.headers["authorization"] == "Bearer " + old_token:
            return httpx.Response(401)
        body = json.loads(request.content)
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        if body["method"] == "tools/call":
            mode = body["params"]["arguments"].get("request_mode", "prepare")
            modes.append(mode)
            if mode == "submit":
                committed.append(json.loads(response.json()["result"]["content"][0]["text"]))
                state["expired"] = True
                raise httpx.ReadError("deliberately lost commit response", request=request)
        return response

    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        auth = ReferenceAuth(profile, client)
        with auth.store.locked():
            auth.store.write(auth._record((old_token, "original-refresh", int(time.time()) + 600)))
        transport = profile_transport(profile, client)
        transport.initialize()
        tool = reference_server(transport)._tool_manager.get_tool("propose_decision")

        def invoke(**arguments):
            result = asyncio.run(tool.run({"project_id": TEST_PROJECT_ID, **arguments}))
            return json.loads(result.content[0].text)

        saved = invoke(
            title="Renew and recover",
            rationale="Recover the same receipt after explicit credential renewal.",
        )
        ref = {
            "operation_id": saved["request"]["operation_id"],
            "payload_digest": saved["request"]["payload_digest"],
        }
        with pytest.raises(ToolError, match="No verified result"):
            invoke(request_mode="submit", **ref)
        assert committed[0]["status"] == "committed"
        monkeypatch.setattr(
            delivery, "run_pre_team_judgment", lambda *_: pytest.fail("Recovery executed")
        )
        with pytest.raises(ToolError, match="No verified result"):
            invoke(request_mode="recover", **ref)
        auth.refresh()
        if revoke:
            _table().delete_item(
                Key={"pk": f"PROJECT#{TEST_PROJECT_ID}", "sk": f"MEMBER#{USER_ID}"}
            )
            with pytest.raises(ToolError, match="No verified result"):
                invoke(request_mode="recover", **ref)
        else:
            assert invoke(request_mode="discover")["requests"] == committed
            recovered = invoke(request_mode="recover", **ref)
            assert recovered == committed[0]
            assert (
                recovered["execution"]["receipt_json"] == committed[0]["execution"]["receipt_json"]
            )
            monkeypatch.setattr(
                cli, "reference_client", lambda: httpx.Client(transport=httpx.MockTransport(wire))
            )
            result = CliRunner().invoke(
                app,
                [
                    "propose-decision",
                    "--reference-profile",
                    str(path),
                    "--request-mode",
                    "recover",
                    "--operation-id",
                    ref["operation_id"],
                    "--payload-digest",
                    ref["payload_digest"],
                ],
            )
            assert result.exit_code == 0, result.output
            assert json.loads(result.output) == committed[0]
    assert len(exchanges) == 1
    assert exchanges[0]["refresh_token"] == "original-refresh"
    assert modes.count("submit") == 1
    assert modes.count("prepare") == 1
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 1
