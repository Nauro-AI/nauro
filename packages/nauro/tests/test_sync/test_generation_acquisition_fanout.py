"""The bounded fetch window of one presign chunk: order, stopping, re-mint and caps."""

from __future__ import annotations

import concurrent.futures
import itertools
import ssl
import threading
import time

import httpx
import pytest

from nauro.sync import generation_acquisition as acquisition
from nauro.sync import transfer
from nauro.sync.generation_acquisition import GenerationAcquisitionError
from nauro.sync.remote import TransferBoundaryError, TransferOperation, classify_status
from tests.conftest import seed_auth_config
from tests.test_sync.test_generation_acquisition import (
    PRESIGN,
    THREE_ARTIFACTS,
    FakeServer,
    _decisions,
    acquire,
)

TIMEOUT = 5.0
WINDOW = acquisition.ACQUISITION_WORKERS


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_auth_config(variant="sync")
    monkeypatch.setenv("NAURO_API_URL", "https://api.test")
    monkeypatch.setenv("NAURO_AUTH0_DOMAIN", "api.test")
    monkeypatch.setenv("NAURO_AUTH0_CLIENT_ID", "test-client")
    monkeypatch.setattr(transfer, "pause", lambda _seconds: None)


def _fault(status: int) -> TransferBoundaryError:
    fault = classify_status(status, operation=TransferOperation.GET)
    return TransferBoundaryError(
        operation=TransferOperation.GET,
        origin="https://objects.test:443",
        kind=fault.kind,
        retry=fault.retry,
        write_outcome=fault.write_outcome,
        status=status,
    )


def _await(predicate, what: str) -> None:
    # Polls a condition another thread makes true; the deadline only ever fails the test.
    deadline = time.monotonic() + TIMEOUT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.001)


class Probe:
    """Counts fetches and exposes each chunk's stop event to the fake fetch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.active = 0
        self.peak = 0
        self.stops: dict[str, threading.Event] = {}
        self.stuck: list[str] = []
        real = transfer.download_with_retry

        def spy(path, urls, fetch, *, stop=None):
            self.stops[path] = stop
            return real(path, urls, fetch, stop=stop)

        monkeypatch.setattr(acquisition, "download_with_retry", spy)

    def enter(self, path: str) -> None:
        with self.lock:
            self.started.append(path)
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1

    def until_stopped(self, path: str) -> None:
        if not self.stops[path].wait(TIMEOUT):
            self.stuck.append(path)


def _in_thread(server: FakeServer) -> tuple[concurrent.futures.Future, concurrent.futures.Executor]:
    runner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    return runner.submit(acquire, server), runner


def test_window_holds_ten_fetches_and_submits_the_next_as_one_completes(monkeypatch) -> None:
    # Without the window every path starts at once and the full-window signal never fires.
    artifacts = _decisions(25)
    server = FakeServer(artifacts)
    probe = Probe(monkeypatch)
    release = {path: threading.Event() for path in artifacts}
    full = threading.Event()
    real_wait = concurrent.futures.wait

    def spy_wait(futures, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if return_when == concurrent.futures.FIRST_COMPLETED and len(futures) == WINDOW:
            full.set()
        return real_wait(futures, timeout=timeout, return_when=return_when)

    def fetch(_session, path, _url):
        probe.enter(path)
        try:
            assert release[path].wait(TIMEOUT), path
            return artifacts[path]
        finally:
            probe.leave()

    monkeypatch.setattr(acquisition, "wait", spy_wait)
    monkeypatch.setattr(acquisition, "_fetch_artifact", fetch)
    result, runner = _in_thread(server)
    try:
        assert full.wait(TIMEOUT)
        _await(lambda: len(probe.started) == WINDOW, "the first window")
        assert sorted(probe.started) == sorted(artifacts)[:WINDOW]
        release[sorted(artifacts)[0]].set()
        _await(lambda: len(probe.started) == WINDOW + 1, "the eleventh fetch")
        assert probe.started[-1] == sorted(artifacts)[WINDOW]
    finally:
        for event in release.values():
            event.set()
        runner.shutdown()
    assert len(result.result(TIMEOUT).artifacts) == 25
    assert (len(probe.started), probe.peak, probe.active) == (25, WINDOW, 0)


def test_artifacts_reach_verification_in_manifest_order_whatever_the_completion_order(
    monkeypatch,
) -> None:
    # Without the order rebuild the first path, finishing last, lands last and the tuple differs.
    artifacts = _decisions(12)
    ordered = sorted(artifacts)
    server = FakeServer(artifacts)
    finished: list[str] = []
    captured: list[tuple[tuple[str, bytes], ...]] = []
    real_done = acquisition._PageUrls.done
    real_verify = acquisition.verify_generation_projection

    def done(self, path):
        real_done(self, path)
        finished.append(path)

    def verify(target, *, manifest_json, artifacts):
        captured.append(artifacts)
        return real_verify(target, manifest_json=manifest_json, artifacts=artifacts)

    def fetch(_session, path, _url):
        if path == ordered[0]:
            _await(lambda: len(finished) == len(ordered) - 1, "the other paths")
        return artifacts[path]

    monkeypatch.setattr(acquisition._PageUrls, "done", done)
    monkeypatch.setattr(acquisition, "verify_generation_projection", verify)
    monkeypatch.setattr(acquisition, "_fetch_artifact", fetch)
    proof = acquire(server)
    assert finished[-1] == ordered[0]
    assert captured == [tuple((path, artifacts[path]) for path in ordered)]
    assert [(a.path, a.content) for a in proof.artifacts] == list(captured[0])


@pytest.mark.parametrize("drained", ["return", "raise"])
def test_first_failure_stops_the_window_and_reaches_the_caller_unchanged(
    monkeypatch, drained: str
) -> None:
    # Without the stop the queued paths fetch after the failure and the started count grows;
    # without first-failure capture a drained error replaces the initiating one.
    artifacts = _decisions(25)
    ordered = sorted(artifacts)
    server = FakeServer(artifacts)
    probe = Probe(monkeypatch)
    initiating = _fault(404)
    attempts: dict[str, int] = {}

    def fetch(_session, path, _url):
        probe.enter(path)
        try:
            attempts[path] = attempts.get(path, 0) + 1
            if path == ordered[5]:
                _await(lambda: len(probe.started) == WINDOW, "a full window")
                raise initiating
            probe.until_stopped(path)
            if drained == "raise":
                raise _fault(503)
            return artifacts[path]
        finally:
            probe.leave()

    monkeypatch.setattr(acquisition, "_fetch_artifact", fetch)
    with pytest.raises(TransferBoundaryError) as caught:
        acquire(server)
    assert caught.value is initiating
    assert probe.active == 0 and probe.stuck == []
    assert sorted(probe.started) == ordered[:WINDOW]
    # A drained transient fault sees the stop and is not retried.
    assert set(attempts.values()) == {1}


def test_certificate_failure_trips_the_origin_and_aborts_the_window(monkeypatch) -> None:
    artifacts = _decisions(25)
    ordered = sorted(artifacts)
    server = FakeServer(artifacts)
    probe = Probe(monkeypatch)

    def hook(_count: int, path: str) -> None:
        probe.enter(path)
        try:
            if path == ordered[0]:
                _await(lambda: len(probe.started) == WINDOW, "a full window")
                cause = ssl.SSLCertVerificationError("cert")
                raise httpx.ConnectError("connect failed") from cause
            probe.until_stopped(path)
        finally:
            probe.leave()

    server.object_hook = hook
    with pytest.raises(TransferBoundaryError) as first:
        acquire(server)
    assert first.value.kind.value == "tls-certificate"
    assert server.counts["object"] == WINDOW and probe.stuck == []
    with pytest.raises(TransferBoundaryError) as second:
        acquire(server)
    assert second.value.kind.value == "origin-aborted"
    assert server.counts["object"] == WINDOW and server.counts["projection"] == 2


class ExpiringServer(FakeServer):
    """Refuses every first-mint URL as expired once both paths are in flight."""

    def __init__(self, artifacts: dict[str, bytes], *, again: bool) -> None:
        super().__init__(artifacts)
        self.again = again
        self.barrier = threading.Barrier(2, timeout=TIMEOUT)
        self.mints: dict[str, list[int]] = {path: [] for path in artifacts}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "objects.test":
            return super().handle(request)
        path = request.url.path.lstrip("/").partition("/")[2]
        mint = int(request.url.params["mint"])
        self.mints[path].append(mint)
        if mint == 1:
            self.barrier.wait()
            return httpx.Response(403)
        if self.again and path == min(self.artifacts):
            return httpx.Response(403)
        return httpx.Response(200, content=self.artifacts[path])


@pytest.mark.parametrize("again", [False, True], ids=["fresh-urls-serve", "second-expiry"])
def test_concurrent_expiry_remints_once_and_keeps_the_per_file_budget(again: bool) -> None:
    # Without the single-flight guard both workers presign and the presign count is three.
    artifacts = _decisions(2)
    first, second = sorted(artifacts)
    server = ExpiringServer(artifacts, again=again)
    if again:
        with pytest.raises(TransferBoundaryError) as caught:
            acquire(server)
        assert caught.value.status == 403
        # The second path may see the stop before its own retry, so only its first mint is fixed.
        assert server.mints[first] == [1, 2] and server.mints[second][0] == 1
    else:
        assert len(acquire(server).artifacts) == 2
        assert server.mints == {first: [1, 2], second: [1, 2]}
    assert [call for call in server.calls() if call == PRESIGN] == [PRESIGN, PRESIGN]


@pytest.mark.parametrize("order", list(itertools.permutations(sorted(THREE_ARTIFACTS))))
@pytest.mark.parametrize("slack", [-1, 0])
def test_total_cap_applies_whatever_the_completion_order(monkeypatch, order, slack) -> None:
    total = sum(len(body) for body in THREE_ARTIFACTS.values())
    monkeypatch.setattr(acquisition, "_MAX_PROJECTION_BYTES", total + slack)
    server = FakeServer(THREE_ARTIFACTS)
    probe = Probe(monkeypatch)
    finished: list[str] = []
    real_done = acquisition._PageUrls.done

    def done(self, path):
        real_done(self, path)
        finished.append(path)

    def fetch(_session, path, _url):
        ahead = order.index(path)
        _await(lambda: len(finished) >= ahead or probe.stops[path].is_set(), "the forced order")
        return THREE_ARTIFACTS[path]

    monkeypatch.setattr(acquisition._PageUrls, "done", done)
    monkeypatch.setattr(acquisition, "_fetch_artifact", fetch)
    if slack < 0:
        with pytest.raises(GenerationAcquisitionError, match="total size cap"):
            acquire(server)
        assert finished == list(order[:2])
    else:
        assert len(acquire(server).artifacts) == 3
        assert finished == list(order)


class _Urls:
    def __init__(self) -> None:
        self.mints = 0

    def url_for(self, path: str) -> str:
        return f"https://objects.test/{path}?mint={self.mints}"

    def remint(self) -> None:
        self.mints += 1


def _failing(status: int, calls: list[str]):
    def fetch(url: str) -> bytes:
        calls.append(url)
        raise _fault(status)

    return fetch


@pytest.mark.parametrize("when", ["before", "during"])
def test_stop_set_around_a_retry_pause_ends_the_retries(monkeypatch, when: str) -> None:
    # Without the stop checks the transient fault is retried to the full budget.
    stop = threading.Event()
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        stop.set()

    monkeypatch.setattr(transfer, "pause", pause)
    if when == "before":
        stop.set()
    calls: list[str] = []
    with pytest.raises(TransferBoundaryError) as caught:
        transfer.download_with_retry("a.md", _Urls(), _failing(503, calls), stop=stop)
    assert caught.value.status == 503
    assert (len(calls), len(pauses)) == (1, 0 if when == "before" else 1)


def test_stop_set_before_an_expiry_skips_the_remint() -> None:
    stop = threading.Event()
    stop.set()
    urls, calls = _Urls(), []
    with pytest.raises(TransferBoundaryError):
        transfer.download_with_retry("a.md", urls, _failing(403, calls), stop=stop)
    assert (len(calls), urls.mints) == (1, 0)


@pytest.mark.parametrize("stop", [None, threading.Event()], ids=["no-stop", "unset-stop"])
def test_unset_stop_keeps_the_budget_and_the_single_remint(monkeypatch, stop) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(transfer, "pause", pauses.append)
    calls: list[str] = []
    with pytest.raises(TransferBoundaryError):
        transfer.download_with_retry("a.md", _Urls(), _failing(503, calls), stop=stop)
    assert (len(calls), len(pauses)) == (transfer._MAX_ATTEMPTS, transfer._MAX_ATTEMPTS - 1)
    urls, expired = _Urls(), []
    with pytest.raises(TransferBoundaryError):
        transfer.download_with_retry("a.md", urls, _failing(403, expired), stop=stop)
    assert (len(expired), urls.mints) == (2, 1)


class RemintingServer(FakeServer):
    """Path A expires; its re-mint blocks until path B's refusal has stopped the window."""

    def __init__(self, artifacts: dict[str, bytes], stops: dict[str, threading.Event]) -> None:
        super().__init__(artifacts)
        self.stops = stops
        self.first, self.second = sorted(artifacts)
        self.second_arrived = threading.Event()
        self.in_remint = threading.Event()
        self.gets: list[tuple[str, int]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "objects.test":
            if request.url.path == "/generations/presign" and self.counts["presign"] == 1:
                self.in_remint.set()
                assert self.stops[self.first].wait(TIMEOUT), "the window never stopped"
            return super().handle(request)
        path = request.url.path.lstrip("/").partition("/")[2]
        self.gets.append((path, int(request.url.params["mint"])))
        if path == self.second:
            self.second_arrived.set()
            assert self.in_remint.wait(TIMEOUT), "the re-mint never started"
            return httpx.Response(404)
        assert self.second_arrived.wait(TIMEOUT), "the second path never arrived"
        mint = self.gets[-1][1]
        return httpx.Response(403) if mint == 1 else httpx.Response(200, content=b"# 0\n")


def test_stop_set_during_a_remint_starts_no_further_get(monkeypatch) -> None:
    # Without the check before each attempt the first path fetches again on the fresh mint.
    probe = Probe(monkeypatch)
    outcomes: dict[str, BaseException] = {}
    spy = acquisition.download_with_retry

    def record(path, urls, fetch, *, stop=None):
        try:
            return spy(path, urls, fetch, stop=stop)
        except BaseException as exc:
            outcomes[path] = exc
            raise

    monkeypatch.setattr(acquisition, "download_with_retry", record)
    server = RemintingServer(_decisions(2), probe.stops)
    with pytest.raises(TransferBoundaryError) as caught:
        acquire(server)
    assert caught.value.status == 404 and caught.value is outcomes[server.second]
    assert [get for get in server.gets if get[0] == server.first] == [(server.first, 1)]
    assert outcomes[server.first].status == 403
    assert server.counts["presign"] == 2


def test_interrupt_in_the_final_wait_stops_workers_mid_pause(monkeypatch) -> None:
    # Without the stop on every exit path each pause runs out and the retries continue.
    artifacts = _decisions(3)
    server = FakeServer(artifacts)
    probe = Probe(monkeypatch)
    interrupt = KeyboardInterrupt()
    lock = threading.Lock()
    attempts: dict[str, int] = {}
    pausing: list[int] = []
    stopped_in_pause: list[bool] = []
    interrupted: list[bool] = []
    real_wait = concurrent.futures.wait

    def fetch(_session, path, _url):
        with lock:
            attempts[path] = attempts.get(path, 0) + 1
        raise _fault(503)

    def pause(_seconds: float) -> None:
        stop = next(iter(probe.stops.values()))
        with lock:
            pausing.append(1)
        stopped = stop.wait(TIMEOUT)
        with lock:
            stopped_in_pause.append(stopped)

    def interrupting_wait(futures, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if not interrupted:
            interrupted.append(True)
            _await(lambda: len(pausing) == len(artifacts), "every worker in its pause")
            raise interrupt
        return real_wait(futures, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(transfer, "pause", pause)
    monkeypatch.setattr(acquisition, "wait", interrupting_wait)
    monkeypatch.setattr(acquisition, "_fetch_artifact", fetch)
    with pytest.raises(KeyboardInterrupt) as caught:
        acquire(server)
    assert caught.value is interrupt
    assert stopped_in_pause == [True] * len(artifacts)
    assert attempts == dict.fromkeys(artifacts, 1)
