"""The store-reading commands serve a generation replica from its installed generation.

``doctor``, ``graph`` and ``log --decisions`` read the committed generation rather than the
vacated legacy root, ``log`` refuses the local snapshot archive, and ``sync --status`` reports
the replica's refresh state. The replica is installed through the real attachment flow and
advanced to a generation that carries decisions, with the server wired in process.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from nauro_core.decision_model import Decision, format_decision
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.generation_authority import RefreshRequiredError
from nauro.store.home import nauro_home
from tests import test_generation_installation as fixtures
from tests.test_stdio_startup_authority import installed as installed_fixture

runner = CliRunner()
GENERATION_ID = "01K77777777777777777777777"


def _decision(num: int, *, supersedes: str | None = None) -> bytes:
    return format_decision(
        Decision(
            date="2026-03-15",
            confidence="high",
            num=num,
            title=f"Decision {num}",
            rationale=f"Rationale for decision {num}.",
            supersedes=supersedes,
        )
    ).encode()


ARTIFACTS = {
    "decisions/001-old.md": _decision(1),
    "decisions/002-new.md": _decision(2, supersedes="1"),
    "decisions/003-broken.md": b"not a decision\n",
    "state.md": b"Committed generation state\n",
}


def _serve(monkeypatch, artifacts: dict[str, bytes]) -> None:
    """Serve ``artifacts`` as the project's current generation from an in-process server."""
    monkeypatch.setattr(fixtures, "GENERATION_ID", GENERATION_ID)
    projection = fixtures._projection(artifacts)
    identity = projection.target.identity.model_dump()
    objects = {f"https://objects.example/{path}": body for path, body in artifacts.items()}

    def wire(request):
        if request.url.path == "/generations/projection":
            manifest = base64.b64encode(projection.manifest_json).decode()
            return httpx.Response(200, json={"projection": identity, "manifest_base64": manifest})
        if request.url.path == "/generations/presign":
            requested = json.loads(request.content)["paths"]
            urls = [{"path": path, "url": f"https://objects.example/{path}"} for path in requested]
            return httpx.Response(
                200,
                json={"projection": identity, "urls": urls, "expires_at": "2999-12-31T23:59:59Z"},
            )
        return httpx.Response(200, content=objects[str(request.url)])

    with httpx.Client(trust_env=False) as client:
        concrete = type(client)
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: concrete(transport=httpx.MockTransport(wire), **kw)
    )


@pytest.fixture
def replica(tmp_path, monkeypatch):
    _repo, binding, *_ = installed_fixture.__wrapped__(tmp_path, monkeypatch)
    _serve(monkeypatch, ARTIFACTS)
    refreshed = runner.invoke(app, ["sync", "--project", binding.project_id])
    assert refreshed.exit_code == 0, refreshed.output
    monkeypatch.setattr("nauro.cli.commands.graph.webbrowser.open", lambda uri: True)
    return binding


def test_doctor_diagnoses_the_installed_generation(replica):
    result = runner.invoke(app, ["doctor", "--project", replica.project_id])
    assert result.exit_code == 0, result.output
    assert f"Generation: {GENERATION_ID}, committed" in result.stdout
    assert "Unparseable decision files (1):" in result.stdout
    assert "003-broken" in result.stdout
    assert "D2 supersedes D1, but D1 is still active" in result.stdout
    assert "no hosted route flips a backref yet" in result.stdout
    assert "run 'nauro repair'" not in result.stdout


def test_doctor_does_not_scan_a_replica_for_unknown_files(replica):
    (replica.store_path / "adoption-review.md").write_text("draft\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--project", replica.project_id])
    assert result.exit_code == 0, result.output
    assert "Unknown store files" not in result.stdout


def test_log_lists_the_installed_generation_decisions(replica):
    result = runner.invoke(app, ["log", "--decisions", "--project", replica.project_id])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"Generation: {GENERATION_ID}, committed")
    assert "Decision 1" in result.stdout
    assert "Decision 2" in result.stdout
    as_json = runner.invoke(app, ["log", "--decisions", "--json", "--project", replica.project_id])
    assert as_json.exit_code == 0, as_json.output
    assert [row["num"] for row in json.loads(as_json.stdout)] == [1, 2]


@pytest.mark.parametrize("flags", [[], ["--json"], ["--full"]])
def test_log_refuses_the_local_snapshot_archive(replica, flags):
    result = runner.invoke(app, ["log", *flags, "--project", replica.project_id])
    assert result.exit_code == 1
    assert "Snapshots of a generation replica stay on the server" in result.stderr
    assert "nauro diff-since-last-session" in result.stderr
    assert result.stdout == ""


def test_graph_renders_the_installed_generation_outside_the_replica_root(replica):
    result = runner.invoke(app, ["graph", "--no-open", "--project", replica.project_id])
    assert result.exit_code == 0, result.output
    expected = nauro_home() / f"nauro-graph-{replica.project_id}.html"
    assert result.stdout.strip() == str(expected.resolve())
    assert "Decision 2" in expected.read_text(encoding="utf-8")
    assert f"Generation: {GENERATION_ID}, committed" in result.stderr
    assert "Skipping unreadable decision file 003-broken.md" in result.stderr
    assert not (replica.store_path / "nauro-graph.html").exists()
    as_json = runner.invoke(app, ["graph", "--format", "json", "--project", replica.project_id])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert sorted(node["number"] for node in payload["nodes"]) == [1, 2]


def test_sync_status_reports_the_replica_refresh(replica):
    result = runner.invoke(app, ["sync", "--status", "--project", replica.project_id])
    assert result.exit_code == 0, result.output
    assert f"Generation: {GENERATION_ID}" in result.stdout
    assert "Last successful refresh:" in result.stdout
    assert "Files tracked" not in result.stdout
    assert "presign" not in result.stdout


@pytest.mark.parametrize(
    "command", [["doctor"], ["log", "--decisions"], ["graph", "--format", "json"]]
)
def test_unreadable_generation_refuses_with_guidance(replica, monkeypatch, command):
    monkeypatch.setattr(
        "nauro.cli.generation_reads.read_generation_guidance",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("private detail")),
    )
    result = runner.invoke(app, [*command, "--project", replica.project_id])
    assert result.exit_code == 1
    assert "The installed generation could not be read" in result.stderr
    assert "private detail" not in result.output


def test_graph_withholds_generation_diagnostics_until_authorized(replica, monkeypatch):
    def refuse(target, session):
        raise RefreshRequiredError("The current authorized projection requires reconciliation.")

    monkeypatch.setattr("nauro.sync.generation_guidance.authorize_installed_target", refuse)
    result = runner.invoke(app, ["graph", "--format", "json", "--project", replica.project_id])
    assert result.exit_code == 1
    assert "The installed generation could not be read" in result.stderr
    assert "003-broken" not in result.output
    assert "reconciliation" not in result.output
