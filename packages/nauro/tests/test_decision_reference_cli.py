import json

import pytest
from typer.testing import CliRunner

from nauro.cli import autogen
from nauro.cli import decision_reference as reference
from nauro.cli.main import app
from nauro.sync.decision_reference import DecisionReferenceError

PROJECT = "01KQ6AZGNA0B3QBF67NBXP3S45"
ACTOR = "01K" + "0" * 21 + "08"
REFERENCE = "decision-request:" + "01K" + "0" * 21 + "09"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"user_id": ACTOR, "access_token": "synthetic"}))
    credentials.chmod(0o600)
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoint": "https://probe.example/mcp",
                "project_id": PROJECT,
                "actor_id": ACTOR,
                "credentials_file": str(credentials),
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        autogen, "resolve_target_project", lambda *_: pytest.fail("Local resolution")
    )
    return path, credentials


def invoke(path, *arguments):
    return CliRunner().invoke(
        app, ["propose-decision", "--reference-profile", str(path), *arguments]
    )


def test_prepare_preserves_explicit_content_and_omits_cli_defaults(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        reference, "_execute", lambda profile, request: calls.append(request) or {"saved": True}
    )
    result = invoke(
        configured[0], "Approved rationale", "--title", "Draft", "--confidence", "medium"
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "project_id": PROJECT,
            "rationale": "Approved rationale",
            "title": "Draft",
            "confidence": "medium",
        }
    ]
    assert json.loads(result.stdout) == {"saved": True}


@pytest.mark.parametrize("mode", ["submit", "recover", "retry"])
def test_reference_only_modes_do_not_receive_default_content(configured, monkeypatch, mode):
    calls = []
    monkeypatch.setattr(reference, "_execute", lambda profile, request: calls.append(request) or {})
    result = invoke(
        configured[0],
        "--request-mode",
        mode,
        "--operation-id",
        REFERENCE,
        "--payload-digest",
        "0" * 64,
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "project_id": PROJECT,
            "request_mode": mode,
            "operation_id": REFERENCE,
            "payload_digest": "0" * 64,
        }
    ]


def test_discovery_cursor_is_forwarded(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(reference, "_execute", lambda profile, request: calls.append(request) or {})
    result = invoke(configured[0], "--request-mode", "discover", "--after", "opaque")
    assert result.exit_code == 0, result.output
    assert calls == [{"project_id": PROJECT, "request_mode": "discover", "after": "opaque"}]


@pytest.mark.parametrize(
    "extra",
    [
        ["changed"],
        ["--title", ""],
        ["--operation", "add"],
        ["--project", "other"],
        ["--format", "text"],
    ],
)
def test_reference_rejects_content_and_local_selectors_before_execution(
    configured, monkeypatch, extra
):
    monkeypatch.setattr(reference, "_execute", lambda *_: pytest.fail("Execution"))
    result = invoke(
        configured[0],
        "--request-mode",
        "recover",
        "--operation-id",
        REFERENCE,
        "--payload-digest",
        "0" * 64,
        *extra,
    )
    assert result.exit_code == 2


def test_transport_failure_never_falls_back_or_retries(configured, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise DecisionReferenceError("sensitive internal value")

    monkeypatch.setattr(reference, "_execute", fail)
    result = invoke(configured[0], "--request-mode", "discover")
    assert result.exit_code == 1
    assert len(calls) == 1
    assert "sensitive internal value" not in result.output
    assert "No retry was sent" in result.output


@pytest.mark.parametrize(
    "change", ["permissions", "symlink", "unknown", "duplicate", "relative_credentials"]
)
def test_invalid_profile_fails_before_execution(configured, monkeypatch, change):
    path, _ = configured
    monkeypatch.setattr(reference, "_execute", lambda *_: pytest.fail("Execution"))
    if change == "permissions":
        path.chmod(0o644)
    elif change == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    elif change == "duplicate":
        path.write_text('{"version":1,"version":1}')
    else:
        value = json.loads(path.read_text())
        value["extra" if change == "unknown" else "credentials_file"] = "relative"
        path.write_text(json.dumps(value))
    assert invoke(path, "--request-mode", "discover").exit_code == 2


def test_reference_mode_without_profile_refuses_local_dispatch(configured):
    result = CliRunner().invoke(app, ["propose-decision", "--request-mode", "discover"])
    assert result.exit_code == 2


def test_credentials_are_reloaded_and_private(configured):
    _, path = configured
    assert reference._credentials(path).access_token == "synthetic"
    path.write_text(json.dumps({"user_id": ACTOR, "access_token": "replacement"}))
    assert reference._credentials(path).access_token == "replacement"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        reference._credentials(path)


def test_unsupported_platform_refuses_before_open(configured, monkeypatch):
    monkeypatch.delattr(reference.os, "O_NOFOLLOW")
    with pytest.raises(ValueError, match="unsupported on this platform"):
        reference._private_json(configured[0])
    assert invoke(configured[0], "--request-mode", "discover").exit_code == 2
