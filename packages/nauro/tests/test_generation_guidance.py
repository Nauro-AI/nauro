import json

import pytest
from nauro_core import operations
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp.payloads import build_guidance_payload
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.sync import generation_guidance as guidance
from tests.test_generation_attachment import hosted as hosted_fixture
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import admitted as admitted_fixture
from tests.test_generation_refresh import _target


@pytest.fixture
def connected(monkeypatch):
    binding, current, checks = admitted_fixture.__wrapped__(monkeypatch)

    class Session:
        actor = USER_ID

        def __init__(self, selected):
            assert selected == binding

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def credentials(self):
            return None

    monkeypatch.setattr(guidance, "GenerationTransferSession", Session)
    monkeypatch.setattr(guidance, "resolve_project_binding", lambda *a, **k: binding)
    return binding, current, checks


def test_guidance_preserves_exact_l0_and_identifies_generation(connected):
    binding, current, checks = connected
    (binding.store_path / "project.md").write_text("UNPUBLISHED FLAT CONTENT")
    text, notice = build_guidance_payload(binding.store_path)
    expected = operations.get_context(GenerationSnapshotStore(current[0]), 0)
    assert text == expected.content
    assert notice == guidance.generation_notice(current[0].target.identity)
    assert "UNPUBLISHED" not in text
    assert len(checks) == 4


@pytest.mark.parametrize("failure", ["scope", "credentials", "render"])
def test_guidance_rechecks_before_return(connected, monkeypatch, failure):
    binding, current, _ = connected

    def render(store):
        if failure == "scope":
            current[0] = _target()
        elif failure == "credentials":

            def expired(self):
                raise ValueError("Expired credentials")

            monkeypatch.setattr(guidance.GenerationTransferSession, "credentials", expired)
        else:
            raise ValueError("Invalid render")
        return "MUST NOT ESCAPE"

    with pytest.raises(PermissionError, match="Generation guidance unavailable"):
        guidance.read_generation_guidance(binding.store_path, render)


def test_stale_guidance_never_renders_or_refreshes(connected, monkeypatch):
    binding, current, _ = connected
    current[0] = _target()

    def forbidden(*args, **kwargs):
        pytest.fail("Stale guidance must not render or refresh")

    monkeypatch.setattr("nauro.sync.generation_refresh.recover_generation_refresh", forbidden)
    with pytest.raises(PermissionError, match="Generation guidance unavailable"):
        guidance.read_generation_guidance(binding.store_path, forbidden)


def test_generation_prompt_uses_verified_snapshot_and_scoped_dedup(connected, monkeypatch):
    from nauro.cli.commands import hook

    binding, _, _ = connected
    monkeypatch.setattr(hook, "_resolve_store_path", lambda cwd: binding.store_path)
    monkeypatch.setattr(hook, "_apply_floor", lambda hits, size: hits)
    seen = []
    monkeypatch.setattr(hook, "_load_seen", lambda session: set())
    monkeypatch.setattr(
        hook, "_record_seen", lambda session, numbers: seen.append((session, numbers))
    )
    payload = json.dumps(
        {
            "cwd": str(binding.store_path),
            "prompt": "Require durability barriers",
            "session_id": "test",
        }
    )
    result = CliRunner().invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0
    assert "Require durability barriers" in result.output
    assert "Derived context from generation" in result.output
    assert len(seen) == 1
    assert len(seen[0][0]) == 64
    assert seen[0][1] == [1]


def test_hook_final_authority_failure_has_no_output_or_dedup(connected, monkeypatch):
    from nauro.cli.commands import hook

    binding, current, _ = connected
    monkeypatch.setattr(hook, "_resolve_store_path", lambda cwd: binding.store_path)
    original = hook._generation_candidates

    def changed(store, prompt):
        result = original(store, prompt)
        current[0] = _target()
        return result

    def forbidden(*args):
        pytest.fail("Failed authorization must not consume dedup state")

    monkeypatch.setattr(hook, "_generation_candidates", changed)
    monkeypatch.setattr(hook, "_record_seen", forbidden)
    payload = json.dumps(
        {
            "cwd": str(binding.store_path),
            "prompt": "Require durability barriers",
            "session_id": "test",
        }
    )
    result = CliRunner().invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0
    assert result.output == ""


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    return hosted_fixture.__wrapped__(tmp_path, monkeypatch)


def test_normal_credentials_generate_saved_guidance_and_bootstrap(hosted):
    from nauro.store.registry import get_store_path_v2
    from nauro.templates.agents_md import regenerate_agents_md_for_project
    from tests.test_generation_attachment import PROJECT_ID, _run

    repo, _, _, _, calls = hosted
    assert _run(repo).exit_code == 0
    calls.clear()
    assert regenerate_agents_md_for_project(PROJECT_ID, get_store_path_v2(PROJECT_ID)) == [repo]
    saved = (repo / "AGENTS.md").read_text()
    assert "Derived context from generation 01K11111111111111111111111" in saved
    assert "Saved guidance does not prove current authorization or freshness." in saved
    assert "skip L0" not in saved
    assert {request.url.path for request in calls} == {"/generations/projection"}
    calls.clear()
    payload = json.dumps({"cwd": str(repo), "hook_event_name": "SessionStart"})
    result = CliRunner().invoke(app, ["hook", "codex-bootstrap"], input=payload)
    assert result.exit_code == 0
    assert "Derived context from generation" in result.output
    assert {request.url.path for request in calls} == {"/generations/projection"}


@pytest.mark.parametrize("change", ["expired", "logout", "revoked"])
def test_saved_guidance_survives_failed_admission(hosted, change):
    from nauro.store.registry import get_store_path_v2
    from nauro.templates.agents_md import regenerate_agents_md_for_project
    from tests.test_generation_attachment import PROJECT_ID, _run

    repo, _, credentials, control, _ = hosted
    assert _run(repo).exit_code == 0
    old = "Preserved owner guidance\n"
    (repo / "AGENTS.md").write_text(old)
    if change == "revoked":
        control["status"] = 403
    else:
        with credentials.locked():
            record = credentials.read()
            credentials.write(
                credentials.empty("logged_out")
                if change == "logout"
                else record.model_copy(update={"expires_at": 1})
            )
    with pytest.raises(PermissionError, match="Generation guidance unavailable"):
        regenerate_agents_md_for_project(
            PROJECT_ID, get_store_path_v2(PROJECT_ID), overwrite_unmanaged=True
        )
    assert (repo / "AGENTS.md").read_text() == old


def test_generation_setup_can_write_guidance_without_legacy_reads(hosted):
    from tests.test_generation_attachment import _run

    repo, _, _, _, calls = hosted
    assert _run(repo).exit_code == 0
    calls.clear()
    result = CliRunner().invoke(app, ["setup", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "Derived context from generation" in (repo / "AGENTS.md").read_text()
    assert (repo / ".mcp.json").is_file()
    assert {request.url.path for request in calls} == {"/generations/projection"}
