from __future__ import annotations

import json

import pytest

from nauro.store import generation_installation as installation
from nauro.store.registry import get_project_entry_v2, get_store_path_v2
from nauro.store.repo_config import repo_config_path
from nauro.sync import generation_attachment as attachment
from nauro.sync import generation_attachment_record as records
from nauro.sync import generation_refresh as refresh
from tests.test_generation_attachment import _run
from tests.test_generation_attachment import hosted as hosted_fixture
from tests.test_generation_installation import PROJECT_ID


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    return hosted_fixture.__wrapped__(tmp_path, monkeypatch)


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _interrupt(repo, monkeypatch, boundary):
    with monkeypatch.context() as fault:
        if boundary in {"root", "registry", "config"}:
            name = {
                "root": "install_generation_root",
                "registry": "bind_project_store_v2",
                "config": "save_repo_config",
            }[boundary]
            original = getattr(attachment, name)

            def interrupt(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt()

            fault.setattr(attachment, name, interrupt)
        elif boundary == "record":
            original = records.durable_replace

            def interrupt(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt()

            fault.setattr(records, "durable_replace", interrupt)
        elif boundary in {"carrier", "pointer", "marker"}:
            original = installation.atomic_write_bytes
            selected = {
                "carrier": "authorization-view.json",
                "pointer": "pointer.json",
                "marker": "authority.json",
            }[boundary]

            def interrupt(path, raw):
                original(path, raw)
                if path.name == selected:
                    raise KeyboardInterrupt()

            fault.setattr(installation, "atomic_write_bytes", interrupt)
        else:
            original = refresh.durable_replace
            selected = {
                "intent": "refresh-intent.json",
                "refresh-carrier": "authorization-view.json",
                "refresh-pointer": "pointer.json",
            }[boundary]

            def interrupt(paths, path, raw):
                original(paths, path, raw)
                if path.name == selected:
                    raise KeyboardInterrupt()

            fault.setattr(refresh, "durable_replace", interrupt)
        result = _run(repo)
        assert result.exit_code == 130, result.output


@pytest.mark.parametrize(
    "boundary",
    [
        "record",
        "root",
        "carrier",
        "pointer",
        "marker",
        "intent",
        "refresh-carrier",
        "refresh-pointer",
        "registry",
        "config",
    ],
)
def test_repeat_completes_recorded_attachment(hosted, monkeypatch, boundary):
    repo, _, _, _, calls = hosted
    _interrupt(repo, monkeypatch, boundary)
    saved = records.record_path(PROJECT_ID).read_bytes()
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert records.record_path(PROJECT_ID).read_bytes() == saved
    assert get_project_entry_v2(PROJECT_ID).mode == "cloud"
    assert json.loads(repo_config_path(repo).read_text())["id"] == PROJECT_ID
    assert {call.url.path for call in calls} <= {
        "/projects",
        "/generations/projection",
        "/generations/presign",
        "/state",
    }


def test_failed_final_barrier_is_repeated_before_success(hosted, monkeypatch):
    repo, _, _, _, _ = hosted
    _interrupt(repo, monkeypatch, "refresh-pointer")
    before = _files(get_store_path_v2(PROJECT_ID))
    with monkeypatch.context() as fault:
        fault.setattr(refresh, "sync_file", lambda *a: (_ for _ in ()).throw(OSError("barrier")))
        result = _run(repo)
    assert result.exit_code == 1, result.output
    assert not repo_config_path(repo).exists()
    assert _files(get_store_path_v2(PROJECT_ID)) == before
    assert _run(repo).exit_code == 0


@pytest.mark.parametrize("change", ["actor", "revoked", "endpoint", "repo", "generation", "scope"])
def test_mismatch_refuses_without_changing_retained_evidence(hosted, monkeypatch, change):
    from nauro.store.config import save_config
    from nauro.store.generation_projection import GenerationProjectionTarget

    repo, _, credentials, control, _ = hosted
    _interrupt(repo, monkeypatch, "pointer")
    before = _files(get_store_path_v2(PROJECT_ID))
    record = records.record_path(PROJECT_ID).read_bytes()
    if change == "actor":
        with credentials.locked():
            credentials.write(
                credentials.read().model_copy(update={"user_id": "01K44444444444444444444444"})
            )
    elif change == "revoked":
        control["role"] = "viewer"
    elif change == "endpoint":
        save_config({"api_url": "https://other.example"})
    elif change == "repo":
        repo = repo.parent / "another"
        repo.mkdir()
    else:
        original = attachment.acquire_generation_projection

        def changed(*args, **kwargs):
            from dataclasses import replace

            value = original(*args, **kwargs)
            delta = (
                {"generation_id": "01K66666666666666666666666"}
                if change == "generation"
                else {"projection_scope_id": "b" * 64}
            )
            return replace(
                value,
                target=GenerationProjectionTarget(
                    value.target.binding, value.target.identity.model_copy(update=delta)
                ),
            )

        monkeypatch.setattr(attachment, "acquire_generation_projection", changed)
    result = _run(repo)
    assert result.exit_code == 1, result.output
    assert not repo_config_path(repo).exists()
    assert _files(get_store_path_v2(PROJECT_ID)) == before
    assert records.record_path(PROJECT_ID).read_bytes() == record


@pytest.mark.parametrize("change", ["missing", "corrupt", "legacy", "pointer", "root", "symlink"])
def test_ambiguous_or_corrupt_evidence_refuses_intact(hosted, monkeypatch, change):
    repo, _, _, _, _ = hosted
    _interrupt(repo, monkeypatch, "pointer")
    root = get_store_path_v2(PROJECT_ID)
    record = records.record_path(PROJECT_ID)
    if change == "missing":
        record.unlink()
    elif change == "corrupt":
        record.write_bytes(b"{}")
        record.chmod(0o600)
    elif change == "legacy":
        (root / "state.md").write_bytes(b"legacy data")
    elif change == "symlink":
        (root / "foreign").symlink_to(repo, target_is_directory=True)
    else:
        name = "pointer.json" if change == "pointer" else "state.md"
        next(root.rglob(name)).write_bytes(b"corrupt")
    before = _files(root)
    result = _run(repo)
    assert result.exit_code == 1, result.output
    assert _files(root) == before
    assert not repo_config_path(repo).exists()


def test_record_durability_failure_precedes_replica_mutation(hosted, monkeypatch):
    repo, _, _, _, _ = hosted
    with monkeypatch.context() as fault:
        fault.setattr(records, "sync_file", lambda *a: (_ for _ in ()).throw(OSError("barrier")))
        result = _run(repo)
    assert result.exit_code == 1, result.output
    assert not get_store_path_v2(PROJECT_ID).exists()
    assert records.read_record(PROJECT_ID) is not None
    assert _run(repo).exit_code == 0


def test_partial_staging_is_preserved_without_cleanup(hosted, monkeypatch):
    repo, _, _, _, _ = hosted
    _interrupt(repo, monkeypatch, "root")
    root = get_store_path_v2(PROJECT_ID)
    staging = next(root.rglob("staging")) / "unfinished"
    staging.mkdir()
    (staging / "evidence").write_bytes(b"interrupted")
    before = _files(root)
    result = _run(repo)
    assert result.exit_code == 1, result.output
    assert _files(root) == before
    assert not repo_config_path(repo).exists()


def test_reauthentication_can_resume_only_the_recorded_actor(hosted, monkeypatch):
    repo, _, credentials, _, _ = hosted
    _interrupt(repo, monkeypatch, "marker")
    with credentials.locked():
        original = credentials.read()
        credentials.write(credentials.empty("logged_out"))
    logins = []

    def login(auth, present_url):
        logins.append(auth.project)
        with credentials.locked():
            credentials.write(original.model_copy(update={"revision": "b" * 64}))

    monkeypatch.setattr(attachment.GenerationAuth, "login", login)
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert logins == [PROJECT_ID]
