from __future__ import annotations

from dataclasses import replace

import pytest

from nauro.store.generation_authority import GenerationAuthorityError, RefreshRequiredError
from nauro.store.generation_projection import GenerationProjectionTarget
from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID
from tests.test_generation_refresh import POSIX
from tests.test_generation_refresh import replica as replica

pytestmark = POSIX


def test_initial_refresh_reuses_bytes_but_checks_current_authority(replica, monkeypatch):
    binding, current = replica
    checks = []
    monkeypatch.setattr(
        refresh, "acquire_generation_projection", lambda *a, **kw: pytest.fail("Second download")
    )
    original = refresh.check_generation_projection

    def check(*a, **kw):
        checks.append(None)
        return original(*a, **kw)

    monkeypatch.setattr(refresh, "check_generation_projection", check)
    prepared = refresh.prepare_initial_generation_refresh(
        binding, actor=USER_ID, acquired=current[0]
    )
    assert len(checks) == 1
    assert prepared.projection == current[0]
    assert refresh.commit_generation_refresh(prepared).read_file("state.md") == "fresh state\n"


@pytest.mark.parametrize("change", ["binding", "actor", "manifest"])
def test_reused_projection_is_reverified(replica, monkeypatch, change):
    binding, current = replica
    acquired = current[0]
    if change == "manifest":
        object.__setattr__(acquired, "manifest_json", b"changed")
    else:
        identity = acquired.target.identity
        target_binding = binding
        if change == "actor":
            identity = identity.model_copy(
                update={"installed_for_user_id": "01K88888888888888888888888"}
            )
        else:
            target_binding = replace(binding, server_url="https://other.example")
        object.__setattr__(acquired, "target", GenerationProjectionTarget(target_binding, identity))
    monkeypatch.setattr(
        refresh, "acquire_generation_projection", lambda *a, **kw: pytest.fail("Fallback download")
    )
    with pytest.raises(GenerationAuthorityError):
        refresh.prepare_initial_generation_refresh(binding, actor=USER_ID, acquired=acquired)


def test_reused_projection_requires_fresh_authorization(replica, monkeypatch):
    binding, current = replica

    def refuse(*a, **kw):
        raise RefreshRequiredError("revoked")

    monkeypatch.setattr(refresh, "check_generation_projection", refuse)
    with pytest.raises(RefreshRequiredError, match="revoked"):
        refresh.prepare_initial_generation_refresh(binding, actor=USER_ID, acquired=current[0])


def test_changed_controls_after_reuse_are_fenced_at_commit(replica):
    binding, current = replica
    prepared = refresh.prepare_initial_generation_refresh(
        binding, actor=USER_ID, acquired=current[0]
    )
    paths = refresh.refresh_paths(binding, USER_ID)
    paths.pointer.write_bytes(b"changed")
    with pytest.raises(GenerationRefreshEvidenceError):
        refresh.commit_generation_refresh(prepared)
    assert paths.pointer.read_bytes() == b"changed"


def test_superseded_acquisition_refuses_instead_of_downloading_replacement(replica, monkeypatch):
    from tests.test_generation_refresh import _target

    binding, current = replica
    acquired = current[0]
    current[0] = _target(generation="01K77777777777777777777777")
    monkeypatch.setattr(
        refresh,
        "acquire_generation_projection",
        lambda *a, **kw: pytest.fail("Replacement download"),
    )
    with pytest.raises(RefreshRequiredError):
        refresh.prepare_initial_generation_refresh(binding, actor=USER_ID, acquired=acquired)
