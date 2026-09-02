from __future__ import annotations

import multiprocessing
from pathlib import Path
from threading import Event
from typing import Protocol

import pytest

import nauro.store.generation_lease as generation_lease
from nauro.store.generation_authority import GenerationProjectionIdentity
from nauro.store.generation_lease import (
    GenerationLeaseBusyError,
    GenerationLeaseUnavailableError,
    generation_cleanup_lease,
    generation_read_lease,
)
from nauro.store.replica_control import ReplicaControlLayout

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K22222222222222222222222"
PROJECTION_SCOPE_ID = "a" * 64


class _ReadyEvent(Protocol):
    def set(self) -> None: ...


def _identity() -> GenerationProjectionIdentity:
    return GenerationProjectionIdentity(
        project_id=PROJECT_ID,
        store_format_version=1,
        generation_id=GENERATION_ID,
        manifest_digest="b" * 64,
        committed_at="2026-09-02T01:02:03.000004Z",
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=PROJECTION_SCOPE_ID,
    )


def _layout(tmp_path: Path) -> tuple[ReplicaControlLayout, GenerationProjectionIdentity]:
    identity = _identity()
    layout = ReplicaControlLayout(tmp_path / PROJECT_ID)
    lease = layout.generation_lease(identity)
    lease.parent.mkdir(parents=True)
    lease.write_bytes(b"")
    return layout, identity


def _hold_read_lease(store_path: str, ready: _ReadyEvent) -> None:
    layout = ReplicaControlLayout(Path(store_path))
    with generation_read_lease(layout, _identity()):
        ready.set()
        Event().wait()


def test_shared_read_leases_coexist_and_block_cleanup(tmp_path: Path) -> None:
    layout, identity = _layout(tmp_path)

    with generation_read_lease(layout, identity):
        with generation_read_lease(layout, identity):
            with pytest.raises(GenerationLeaseBusyError) as raised:
                with generation_cleanup_lease(layout, identity):
                    pass

    assert raised.value.code == "generation_lease_busy"
    with generation_cleanup_lease(layout, identity):
        pass


def test_cleanup_lease_blocks_readers_and_other_cleanup(tmp_path: Path) -> None:
    layout, identity = _layout(tmp_path)

    with generation_cleanup_lease(layout, identity):
        with pytest.raises(GenerationLeaseBusyError):
            with generation_read_lease(layout, identity):
                pass
        with pytest.raises(GenerationLeaseBusyError):
            with generation_cleanup_lease(layout, identity):
                pass


@pytest.mark.parametrize("defect", ["missing", "nonempty", "directory", "symlink"])
def test_lease_evidence_must_be_an_empty_regular_file(tmp_path: Path, defect: str) -> None:
    layout, identity = _layout(tmp_path)
    lease = layout.generation_lease(identity)
    lease.unlink()
    if defect == "nonempty":
        lease.write_bytes(b"occupied")
    elif defect == "directory":
        lease.mkdir()
    elif defect == "symlink":
        outside = tmp_path / "outside.lock"
        outside.write_bytes(b"")
        try:
            lease.symlink_to(outside)
        except OSError:
            pytest.skip("platform does not permit test symlinks")

    with pytest.raises(GenerationLeaseUnavailableError) as raised:
        with generation_read_lease(layout, identity):
            pass

    assert raised.value.code == "generation_lease_unavailable"


def test_backend_failure_is_typed_and_closes_the_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, identity = _layout(tmp_path)
    observed = None

    def fail_backend(handle: object, flags: object) -> None:
        nonlocal observed
        observed = handle
        raise ImportError("shared locks unavailable")

    monkeypatch.setattr(generation_lease, "lock", fail_backend)

    with pytest.raises(GenerationLeaseUnavailableError):
        with generation_read_lease(layout, identity):
            pass

    assert observed is not None
    assert observed.closed


def test_body_failure_is_preserved_and_releases_the_lease(tmp_path: Path) -> None:
    layout, identity = _layout(tmp_path)

    with pytest.raises(RuntimeError, match="body failed"):
        with generation_read_lease(layout, identity):
            raise RuntimeError("body failed")

    with generation_cleanup_lease(layout, identity):
        pass


def test_process_exit_releases_lifetime_lease(tmp_path: Path) -> None:
    layout, identity = _layout(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_read_lease, args=(str(layout.store_path), ready))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(GenerationLeaseBusyError):
            with generation_cleanup_lease(layout, identity):
                pass
    finally:
        process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)

    assert not process.is_alive()
    with generation_cleanup_lease(layout, identity):
        pass
