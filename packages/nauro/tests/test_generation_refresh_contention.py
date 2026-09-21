from __future__ import annotations

import multiprocessing
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from filelock import FileLock

from nauro.sync import generation_decision as decisions
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID
from tests.test_generation_refresh import POSIX, _bootstrap
from tests.test_generation_refresh import replica as replica
from tests.test_stdio_startup_authority import installed

__all__ = ["installed"]

pytestmark = POSIX


@contextmanager
def _held_elsewhere(path):
    ready, release = threading.Event(), threading.Event()

    def hold():
        with FileLock(str(path), timeout=0):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert ready.wait(2)
        yield
    finally:
        release.set()
        thread.join(2)


def _evidence(store: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(store)): p.read_bytes()
        for p in store.rglob("*")
        if p.is_file() and not p.name.endswith(".lock")
    }


@pytest.mark.parametrize("phase", ["prepare", "admit", "commit", "install"])
def test_control_contention_refuses_without_changing_replica(replica, phase):
    binding, _ = replica
    prepared = _bootstrap(binding)
    before = _evidence(binding.store_path)
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    lock = binding.store_path / ".replica-control.lock"

    def attempt():
        held = ExitStack()
        if phase == "install":
            authorize = refresh._authorize

            def hold_after_authorization(*args):
                authorize(*args)
                held.enter_context(_held_elsewhere(lock))

            refresh._authorize = hold_after_authorization
        else:
            held.enter_context(_held_elsewhere(lock))
        try:
            if phase == "prepare":
                refresh.prepare_generation_refresh(binding, actor=USER_ID)
            elif phase == "admit":
                refresh.admit_generation_store(binding, actor=USER_ID)
            else:
                refresh.commit_generation_refresh(prepared)
        except Exception as exc:
            result.put((type(exc).__name__, getattr(exc, "code", None)))
        else:
            result.put(("unexpected_success", None))
        finally:
            held.close()

    process = context.Process(target=attempt)
    process.start()
    process.join(5)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(5)
    try:
        assert timed_out is False, "Replica control acquisition waited indefinitely"
        assert process.exitcode == 0
        assert result.get(timeout=1) == ("ReplicaControlBusyError", "generation_control_busy")
    finally:
        result.close()
        result.join_thread()
    assert _evidence(binding.store_path) == before
    store = refresh.commit_generation_refresh(prepared)
    assert store.read_file("state.md") == "fresh state\n"


def test_busy_receipt_refresh_preserves_committed_result(installed, monkeypatch):
    _, binding, connection, _, _, _, _ = installed
    before = _evidence(binding.store_path)
    receipt = {"status": "committed", "execution": {"receipt_json": "exact verified receipt"}}
    calls = []

    def committed(self, **request):
        calls.append(request)
        return receipt

    monkeypatch.setattr(decisions.DecisionReferenceTransport, "propose_decision", committed)
    context = multiprocessing.get_context("fork")
    result = context.Queue()

    def attempt():
        with _held_elsewhere(binding.store_path / ".replica-control.lock"):
            response = decisions.execute_decision(
                (connection, binding.project_id), {"request_mode": "submit"}
            )
            result.put((response, calls))

    process = context.Process(target=attempt)
    process.start()
    process.join(5)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(5)
    try:
        assert timed_out is False, "Committed result waited indefinitely on local refresh"
        assert process.exitcode == 0
        response, submitted = result.get(timeout=1)
    finally:
        result.close()
        result.join_thread()
    assert response["status"] == "committed"
    assert response["execution"] == receipt["execution"]
    assert response["replica_status"]["error_code"] == "receipt_refresh_required"
    assert submitted == [{"request_mode": "submit"}]
    assert _evidence(binding.store_path) == before
