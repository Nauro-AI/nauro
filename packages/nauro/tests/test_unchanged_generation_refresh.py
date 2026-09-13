from __future__ import annotations

import pytest

from nauro.store.generation_authority import GenerationAuthorityError, RefreshRequiredError
from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID
from tests.test_generation_refresh import POSIX, _active, _bootstrap, _target
from tests.test_generation_refresh import replica as replica

pytestmark = POSIX


def _complete(binding, monkeypatch):
    refresh.commit_generation_refresh(_bootstrap(binding))
    monkeypatch.setattr(
        refresh, "acquire_generation_projection", lambda *a, **kw: pytest.fail("Downloaded target")
    )
    return _active(binding)


def test_unchanged_refresh_preserves_intent_and_repeats_barriers(replica, monkeypatch):
    binding, _ = replica
    paths, before, _ = _complete(binding, monkeypatch)
    synced = []
    original = refresh.sync_file

    def sync(paths, path):
        synced.append(path)
        original(paths, path)

    monkeypatch.setattr(refresh, "sync_file", sync)
    result = refresh.recover_generation_refresh(binding, actor=USER_ID)
    assert result.read_file("state.md") == "fresh state\n"
    assert paths.intent.read_bytes() == before
    assert {paths.marker, paths.pointer, paths.carrier, paths.intent} <= set(synced)


@pytest.mark.parametrize("file", ["artifact", "manifest", "intent", "pointer", "carrier"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_invalid_installed_evidence_never_falls_back_to_download(
    replica, monkeypatch, file, damage
):
    binding, current = replica
    paths, _, _ = _complete(binding, monkeypatch)
    root = refresh._layout(binding.store_path, current[0].target).root_path
    target = {
        "artifact": root / "store/state.md",
        "manifest": root / "manifest.json",
        "intent": paths.intent,
        "pointer": paths.pointer,
        "carrier": paths.carrier,
    }[file]
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"broken")
    with pytest.raises(GenerationAuthorityError):
        refresh.recover_generation_refresh(binding, actor=USER_ID)
    assert target.exists() is (damage == "corrupt")
    if damage == "corrupt":
        assert target.read_bytes() == b"broken"


def test_revocation_precedes_local_capture(replica, monkeypatch):
    binding, _ = replica
    _complete(binding, monkeypatch)
    monkeypatch.setattr(refresh, "_capture", lambda *a: pytest.fail("Read revoked root"))

    def refuse(*a, **kw):
        raise RefreshRequiredError("revoked")

    monkeypatch.setattr(refresh, "check_generation_projection", refuse)
    with pytest.raises(RefreshRequiredError, match="revoked"):
        refresh.recover_generation_refresh(binding, actor=USER_ID)


@pytest.mark.parametrize("when", ["authorization", "capture"])
def test_control_change_during_preparation_refuses(replica, monkeypatch, when):
    binding, _ = replica
    paths, _, _ = _complete(binding, monkeypatch)
    name = "check_generation_projection" if when == "authorization" else "_capture"
    original = getattr(refresh, name)

    def change(*a, **kw):
        result = original(*a, **kw)
        paths.carrier.write_bytes(b"changed")
        return result

    monkeypatch.setattr(refresh, name, change)
    with pytest.raises(GenerationRefreshEvidenceError, match="evidence changed"):
        refresh.prepare_generation_refresh(binding, actor=USER_ID)
    assert paths.carrier.read_bytes() == b"changed"


def test_generation_advancing_after_capture_refuses_commit(replica, monkeypatch):
    binding, current = replica
    paths, before, _ = _complete(binding, monkeypatch)
    prepared = refresh.prepare_generation_refresh(binding, actor=USER_ID)
    current[0] = _target(generation="01K77777777777777777777777")
    with pytest.raises(RefreshRequiredError):
        refresh.commit_generation_refresh(prepared)
    assert paths.intent.read_bytes() == before


def test_unchanged_target_still_refuses_failed_final_barrier(replica, monkeypatch):
    binding, _ = replica
    paths, before, _ = _complete(binding, monkeypatch)
    original = refresh.sync_file

    def fail(paths, path):
        if path == paths.pointer:
            raise OSError("barrier failed")
        original(paths, path)

    monkeypatch.setattr(refresh, "sync_file", fail)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.recover_generation_refresh(binding, actor=USER_ID)
    assert paths.intent.read_bytes() == before
    monkeypatch.setattr(refresh, "sync_file", original)
    assert (
        refresh.recover_generation_refresh(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )


@pytest.mark.parametrize("change", ["generation", "scope"])
def test_changed_identity_uses_existing_acquisition(replica, monkeypatch, change):
    binding, current = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    current[0] = (
        _target(generation="01K77777777777777777777777")
        if change == "generation"
        else _target(scope="c" * 64)
    )
    downloaded = []

    def acquire(*a, **kw):
        downloaded.append(current[0].target)
        return current[0]

    monkeypatch.setattr(refresh, "acquire_generation_projection", acquire)
    monkeypatch.setattr(refresh, "_capture", lambda *a: pytest.fail("Captured obsolete root"))
    result = refresh.recover_generation_refresh(binding, actor=USER_ID)
    assert downloaded == [current[0].target]
    assert result.target == current[0].target
