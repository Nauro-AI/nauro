import json

import httpx
import pytest
from mcp_server.generations import read_generation_pointer
from nauro.cli import decision_reference as reference
from nauro.cli.main import app
from tests.conftest import TEST_PROJECT_ID
from tests.test_decision_reference import ORIGIN, probe, signed_transport
from tests.test_judgment_planning import USER_ID
from typer.testing import CliRunner

__all__ = ["probe", "signed_transport"]


@pytest.fixture
def cli(probe, tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps({"user_id": USER_ID, "access_token": probe.headers["Authorization"][7:]})
    )
    credentials.chmod(0o600)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoint": ORIGIN + "/mcp",
                "project_id": TEST_PROJECT_ID,
                "actor_id": USER_ID,
                "credentials_file": str(credentials),
            }
        )
    )
    profile.chmod(0o600)
    control = {"lose": None, "calls": []}

    def wire(request):
        body = json.loads(request.content)
        response = probe.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        if body["method"] == "tools/call":
            assert response.status_code == 200, response.text
            assert "error" not in response.json(), response.text
            mode = body["params"]["arguments"].get("request_mode", "prepare")
            control["calls"].append(mode)
            if mode == control["lose"]:
                raise httpx.ReadError("lost", request=request)
        return response

    monkeypatch.setattr(
        reference, "reference_client", lambda: httpx.Client(transport=httpx.MockTransport(wire))
    )

    def invoke(*arguments):
        return CliRunner().invoke(
            app, ["propose-decision", "--reference-profile", str(profile), *arguments]
        )

    return invoke, control


def selected(invoke, saved, mode):
    return invoke(
        "--request-mode",
        mode,
        "--operation-id",
        saved["request"]["operation_id"],
        "--payload-digest",
        saved["request"]["payload_digest"],
    )


@pytest.mark.parametrize("lost_mode", ["prepare", "submit"])
def test_cli_discovers_and_recovers_lost_response_without_resend(cli, lost_mode):
    invoke, control = cli
    if lost_mode == "submit":
        prepared = invoke(
            "Preserve request identity across interrupted command execution.",
            "--title",
            "CLI decision",
        )
        assert prepared.exit_code == 0, (prepared.output, prepared.exception)
        saved = json.loads(prepared.stdout)
        control["lose"] = "submit"
        failed = selected(invoke, saved, "submit")
    else:
        control["lose"] = "prepare"
        failed = invoke(
            "Preserve request identity across interrupted command execution.",
            "--title",
            "CLI decision",
        )
    assert failed.exit_code == 1
    control["lose"] = None
    discovery = invoke("--request-mode", "discover")
    assert discovery.exit_code == 0, discovery.output
    found = json.loads(discovery.stdout)["requests"]
    assert len(found) == 1
    recovered = selected(invoke, found[0], "recover")
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.stdout) == found[0]
    assert found[0]["status"] == ("committed" if lost_mode == "submit" else "prepared")
    assert control["calls"] == (
        ["prepare", "submit", "discover", "recover"]
        if lost_mode == "submit"
        else ["prepare", "discover", "recover"]
    )
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == (
        1 if lost_mode == "submit" else 0
    )


def test_cli_stale_requires_new_reference_and_repeated_submit_replays(cli):
    invoke, _ = cli
    drafts = []
    for title in ["First CLI decision", "Second CLI decision"]:
        result = invoke(
            "Preserve request identity across interrupted command execution.",
            "--title",
            title,
        )
        assert result.exit_code == 0, result.output
        drafts.append(json.loads(result.stdout))
    winner = selected(invoke, drafts[0], "submit")
    assert winner.exit_code == 0, winner.output
    assert json.loads(winner.stdout)["status"] == "committed"
    repeat = selected(invoke, drafts[0], "submit")
    assert repeat.exit_code == 0
    assert repeat.stdout == winner.stdout
    stale = selected(invoke, drafts[1], "submit")
    assert stale.exit_code == 0
    assert json.loads(stale.stdout)["status"] == "stale"
    fresh = invoke(
        "Preserve request identity across interrupted command execution.",
        "--title",
        "Second CLI decision",
    )
    assert fresh.exit_code == 0
    saved = json.loads(fresh.stdout)
    assert saved["request"]["operation_id"] != drafts[1]["request"]["operation_id"]
    assert saved["effective_draft"]["base_decision_counter"] == 1
    assert selected(invoke, saved, "submit").exit_code == 0
    assert read_generation_pointer(TEST_PROJECT_ID).decision_counter == 2
