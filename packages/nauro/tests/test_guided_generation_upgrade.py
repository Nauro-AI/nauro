from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from nauro.cli import generation_upgrade as upgrade
from nauro.store.migration_admission import MigrationAdmissionError, inspect_migration
from nauro.sync import migration_installation as installation
from nauro.sync.migration_admission import decide_migration_assessment, load_migration_plan
from tests import test_migration_preservation as seed


@pytest.fixture
def assessed(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "decide_migration_assessment", lambda record, **kw: record)
    yield from seed.saved.__wrapped__(tmp_path, monkeypatch)


def test_guided_conversion_requires_one_informed_confirmation(assessed):
    record, plan, session, _, _, calls = assessed
    messages, prompts = [], []

    def confirm(prompt):
        prompts.append(prompt)
        assert calls == []
        assert inspect_migration(session.binding.store_path) == record
        text = "\n".join(messages)
        assert '"decisions/002-two.md": local-only' in text
        assert "outside the active record" in text
        assert "not appear in ordinary project reads" in text
        assert "not imported into hosted history" in text
        assert "Nothing is merged or submitted" in text
        assert "unpublished decision" not in text
        assert json.dumps(plan.backup_directory_name) in text
        assert "unsupported, preserved outside the active record with export" in text
        return True

    result = upgrade.guided_existing_hosted_upgrade(session, emit=messages.append, confirm=confirm)
    assert result.phase == "completed"
    assert result.migration_id == record.migration_id
    assert len(prompts) == 1
    assert (plan.backup_root / "plan.json").read_bytes() == plan.manifest_json
    assert (
        messages[-2] == "This computer now reads the verified hosted record. The original "
        "local files remain preserved."
    )
    assert messages[-1].startswith("Preserved files: ")


def test_deferral_makes_no_hosted_calls_and_keeps_source(assessed):
    record, plan, session, _, _, calls = assessed
    before = {p: p.read_bytes() for p in session.binding.store_path.rglob("*") if p.is_file()}
    messages = []
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: False
    )
    assert result.phase == "declined"
    assert result.migration_id == record.migration_id
    assert calls == []
    assert not plan.backup_root.exists()
    assert {
        p: p.read_bytes() for p in session.binding.store_path.rglob("*") if p.is_file()
    } == before


def test_reopen_interruption_inspects_before_explicit_continuation(assessed, monkeypatch):
    record, plan, session, _, _, calls = assessed
    blocked = decide_migration_assessment(record, preserve=True)
    original = installation._install

    def interrupted(*args):
        raise OSError("disk full")

    monkeypatch.setattr(installation, "_install", interrupted)
    messages = []
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: True
    )
    assert result.phase == "installing"
    assert result.migration_id == blocked.migration_id
    assert any("Upgrade incomplete: disk full" in message for message in messages)
    assert not any("now reads" in message for message in messages)
    count = len(calls)
    current, raw = load_migration_plan(session.binding.store_path)
    assert raw == plan.manifest_json
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: False
    )
    assert result == current
    assert len(calls) == count
    monkeypatch.setattr(installation, "_install", original)
    assert (
        upgrade.guided_existing_hosted_upgrade(
            session, emit=messages.append, confirm=lambda prompt: True
        ).phase
        == "completed"
    )


def test_completed_discovery_is_read_only_and_does_not_claim_current_access(assessed, monkeypatch):
    _, _, session, _, _, calls = assessed
    completed = upgrade.guided_existing_hosted_upgrade(
        session, emit=lambda text: None, confirm=lambda prompt: True
    )
    count = len(calls)

    def forbidden(*args, **kwargs):
        pytest.fail("Completed discovery executed or requested consent")

    monkeypatch.setattr(upgrade, "continue_migration_installation", forbidden)
    messages = []
    assert (
        upgrade.guided_existing_hosted_upgrade(session, emit=messages.append, confirm=forbidden)
        == completed
    )
    assert len(calls) == count
    assert messages == [
        "This computer has a completed upgrade record. Use the normal read or "
        "sync path to verify current access."
    ]


def test_changed_source_keeps_assessed_identity_when_reassessment_is_declined(assessed):
    record, plan, session, *_ = assessed
    (session.binding.store_path / "state_current.md").write_text("Changed after preparation")
    messages = []
    choices = iter([True, False])
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: next(choices)
    )
    assert result == record
    assert any("fresh assessment" in message for message in messages)
    assert not plan.backup_root.exists()


def test_explicit_fresh_assessment_after_deferral(assessed):
    record, plan, session, *_ = assessed
    decide_migration_assessment(record, preserve=False)
    prompts = []

    def consent(prompt):
        prompts.append(prompt)
        return len(prompts) == 1

    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=lambda text: None, confirm=consent
    )
    assert result.phase == "declined"
    assert result.migration_id != record.migration_id
    assert result.predecessor_digest is not None
    assert len(prompts) == 2
    assert not plan.backup_root.exists()


def test_drift_reassessment_requires_its_own_disposition(assessed):
    record, plan, session, *_ = assessed
    (session.binding.store_path / "state_current.md").write_text("New local evidence")
    choices = iter([True, True, False])
    prompts = []

    def choose(prompt):
        prompts.append(prompt)
        return next(choices)

    result = upgrade.guided_existing_hosted_upgrade(session, emit=lambda text: None, confirm=choose)
    assert result.phase == "declined"
    assert result.migration_id != record.migration_id
    assert result.predecessor_digest is not None
    assert len(prompts) == 3
    assert "does not approve conversion" in prompts[1]
    assert not plan.backup_root.exists()


def test_lost_preparation_response_is_discovered_without_new_identity(assessed, monkeypatch):
    from nauro.store.migration_admission import admission_path

    record, _, session, _, _, calls = assessed
    admission_path(session.binding.store_path).unlink()
    original = upgrade.save_migration_assessment

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("preparation response lost")

    monkeypatch.setattr(upgrade, "save_migration_assessment", lost)
    with pytest.raises(OSError, match="preparation response lost"):
        upgrade.guided_existing_hosted_upgrade(
            session,
            emit=lambda text: None,
            confirm=lambda prompt: pytest.fail("No prompt before prepared"),
        )
    saved, _ = load_migration_plan(session.binding.store_path)
    assert saved.phase == "assessed"
    assert saved.migration_id != record.migration_id
    count = len(calls)
    monkeypatch.setattr(
        upgrade, "save_migration_assessment", lambda *a, **kw: pytest.fail("New preparation")
    )
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=lambda text: None, confirm=lambda prompt: False
    )
    assert result.migration_id == saved.migration_id
    assert result.phase == "declined"
    assert len(calls) == count


def test_unknown_staging_reports_incomplete_and_preserves_evidence(assessed, monkeypatch):
    _, _, session, *_ = assessed
    original = installation.install_generation_root

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        staging = next(session.binding.store_path.rglob("staging")) / "unfinished"
        staging.mkdir()
        (staging / "evidence").write_bytes(b"retained")
        raise OSError("interrupted root")

    monkeypatch.setattr(installation, "install_generation_root", interrupted)
    messages = []
    current = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: True
    )
    assert current.phase == "installing"
    monkeypatch.setattr(
        installation,
        "install_generation_root",
        lambda *a, **kw: pytest.fail("Unknown evidence reached installer"),
    )
    messages.clear()
    assert (
        upgrade.guided_existing_hosted_upgrade(
            session, emit=messages.append, confirm=lambda prompt: True
        )
        == current
    )
    assert any("Unrecognized attachment evidence" in message for message in messages)
    assert not any("now reads" in message for message in messages)
    assert next(session.binding.store_path.rglob("evidence")).read_bytes() == b"retained"


def _tree(root):
    return {p: p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def _forbidden(*args, **kwargs):
    pytest.fail("Changed or foreign upgrade executed work")


def _move_target(assessed):
    _, plan, _, control, *_ = assessed
    original = plan.assessment.projection
    manifest = json.loads(original.manifest_json)
    manifest["generation_id"] = "01K55555555555555555555555"
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    identity = original.target.identity.model_copy(
        update={
            "generation_id": manifest["generation_id"],
            "manifest_digest": hashlib.sha256(raw).hexdigest(),
        }
    )
    control["identity"] = identity.model_dump()
    control["manifest"] = raw
    return identity


def test_changed_target_offers_reassessment_before_fresh_consent(assessed, monkeypatch):
    record, plan, session, *_ = assessed
    identity = _move_target(assessed)
    monkeypatch.setattr(upgrade, "continue_migration_installation", _forbidden)
    messages = []
    choices = iter([True, False])
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: next(choices)
    )
    assert result == record
    assert inspect_migration(session.binding.store_path) == record
    assert any("The hosted generation changed" in message for message in messages)
    assert not plan.backup_root.exists()
    prompts = []
    choices = iter([True, True, False])

    def choose(prompt):
        prompts.append(prompt)
        return next(choices)

    result = upgrade.guided_existing_hosted_upgrade(session, emit=lambda text: None, confirm=choose)
    assert result.phase == "declined"
    assert result.migration_id != record.migration_id
    assert "does not approve conversion" in prompts[1]
    assert prompts[2] == prompts[0]
    _, raw = load_migration_plan(session.binding.store_path)
    assert json.loads(raw)["projection"]["generation_id"] == identity.generation_id
    assert not plan.backup_root.exists()


@pytest.mark.parametrize("case", ["blocked", "installing", "blocked_source"])
def test_changed_admitted_upgrade_requires_owner_recovery(assessed, monkeypatch, case):
    record, plan, session, *_ = assessed
    store = session.binding.store_path
    if case == "installing":

        def interrupted(*args):
            raise OSError("interrupted")

        monkeypatch.setattr(installation, "_install", interrupted)
        upgrade.guided_existing_hosted_upgrade(
            session, emit=lambda text: None, confirm=lambda prompt: True
        )
    else:
        decide_migration_assessment(record, preserve=True)
    saved, _ = load_migration_plan(store)
    assert saved.phase == case.split("_")[0]
    if case == "blocked_source":
        (store / "state_current.md").write_text("Changed while blocked")
    else:
        _move_target(assessed)
    before = _tree(store.parent)
    monkeypatch.setattr(upgrade, "continue_migration_installation", _forbidden)
    monkeypatch.setattr(upgrade, "decide_migration_assessment", _forbidden)
    messages = []
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: True
    )
    assert result == saved
    assert inspect_migration(store) == saved
    assert _tree(store.parent) == before
    assert messages[-1] == (
        "This saved upgrade cannot continue against the changed record. Local "
        "writes remain blocked and preserved evidence is retained. Owner "
        "recovery is required."
    )
    assert not any("Reopen" in m or "fresh assessment" in m for m in messages)


@pytest.mark.parametrize(
    "update", [{"actor": "another-owner"}, {"endpoint": "https://other.example"}]
)
def test_saved_upgrade_for_another_connection_is_refused_offline(assessed, monkeypatch, update):
    record, _, session, _, _, calls = assessed
    foreign = record.model_copy(update=update)
    monkeypatch.setattr(upgrade, "inspect_migration", lambda path: foreign)
    monkeypatch.setattr(upgrade, "load_migration_plan", lambda path: (foreign, b""))
    before = _tree(session.binding.store_path.parent)
    with pytest.raises(MigrationAdmissionError, match="another connection"):
        upgrade.guided_existing_hosted_upgrade(session, emit=_forbidden, confirm=_forbidden)
    assert calls == []
    assert _tree(session.binding.store_path.parent) == before
    assert inspect_migration(session.binding.store_path) == record


def test_transport_failure_mid_conversion_is_incomplete_without_cleanup(assessed, monkeypatch):
    _, _, session, *_ = assessed
    root = session.binding.store_path.parent
    transport = session.client._transport
    handler, install = transport.handler, installation.install_generation_root
    seen = {}

    def installed(*args, **kwargs):
        result = install(*args, **kwargs)
        seen["root"] = _tree(root)
        return result

    def dropped(request):
        if "root" in seen:
            seen.setdefault("dropped", _tree(root))
            raise httpx.ConnectError("network dropped", request=request)
        return handler(request)

    monkeypatch.setattr(installation, "install_generation_root", installed)
    monkeypatch.setattr(transport, "handler", dropped)
    messages = []
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: True
    )
    assert result.phase == "installing"
    assert inspect_migration(session.binding.store_path) == result
    assert "Upgrade incomplete: network dropped" in messages
    assert not any("now reads" in message for message in messages)
    assert any("staging" in str(path) for path in seen["dropped"])
    assert _tree(root) == seen["dropped"]


def test_refused_owner_check_during_continuation_is_incomplete(assessed, monkeypatch):
    _, _, session, *_ = assessed
    store = session.binding.store_path
    original = installation._install

    def interrupted(*args):
        raise OSError("interrupted")

    monkeypatch.setattr(installation, "_install", interrupted)
    upgrade.guided_existing_hosted_upgrade(
        session, emit=lambda text: None, confirm=lambda prompt: True
    )
    monkeypatch.setattr(installation, "_install", original)
    saved, _ = load_migration_plan(store)
    assert saved.phase == "installing"
    transport = session.client._transport
    handler = transport.handler

    def refused(request):
        if request.url.path == "/projects":
            return httpx.Response(403, json={"error": "forbidden"})
        return handler(request)

    monkeypatch.setattr(transport, "handler", refused)
    before = _tree(store.parent)
    messages = []
    result = upgrade.guided_existing_hosted_upgrade(
        session, emit=messages.append, confirm=lambda prompt: True
    )
    assert result == saved
    assert inspect_migration(store) == saved
    assert _tree(store.parent) == before
    assert "Upgrade incomplete: Current owner access could not be confirmed." in messages
    assert messages[-1].startswith("Retained evidence was not discarded.")
    assert not any("now reads" in message for message in messages)
