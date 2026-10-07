"""Bounded automatic credential acquisition before generation operations."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from nauro.auth import ActiveCredentials
from nauro.sync.auth_errors import AuthenticationError, ExchangeNotSentError, auth_error_message
from nauro.sync.decision_profile import _private_json
from nauro.sync.generation_credentials import (
    AccountRecord,
    GenerationAuth,
    GenerationConnection,
    generation_credentials,
)
from nauro.sync.reference_auth import AUTH_ERRORS
from nauro.sync.reference_credentials import CredentialRecord, CredentialStore

RENEWAL_WINDOW_SECONDS = 60
RENEWAL_TIMEOUT_SECONDS = 8.0
RETRY_DELAY_SECONDS = 30
# The worker stops its own network work this long before the supervisor's kill. A
# request that never connects restores the saved credentials; once a request is sent,
# a stalled response is an uncertain exchange and clears them like any other failure.
EXCHANGE_MARGIN_SECONDS = 1.5
TIMEOUT_MESSAGE = "Credential renewal timed out. Run 'nauro auth refresh' to recover."
FAILURE_MESSAGE = "Credential renewal failed. Run 'nauro auth refresh' to recover."
# The worker clocks its own startup before the imports that dominate it.
WORKER_CODE = (
    "import time\n"
    "started = time.monotonic()\n"
    "from nauro.sync.generation_renewal import main\n"
    "main(started)\n"
)


class RenewalError(ValueError):
    """Automatic renewal produced no usable credentials; the message names the recovery."""


class RenewalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    connection: GenerationConnection
    project: str
    actor: str
    subject: str
    revision: str
    budget: float  # seconds the worker may spend; it stops before the supervisor's kill


class RenewalOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    error: str | None = None


class RenewalRetry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    actor: str
    subject: str
    retry_after: int


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RenewalError(TIMEOUT_MESSAGE)
    return remaining


def _final_wait(deadline: float) -> float:
    # One bounded read is always allowed after the worker, even at the end of the budget.
    return max(0.1, deadline - time.monotonic())


def renewal_deadline() -> float:
    return time.monotonic() + RENEWAL_TIMEOUT_SECONDS


def _worker_command() -> list[str]:
    # Isolated mode ignores PYTHONPATH, user site and the working directory, so no file
    # in the caller's repository is imported, or run as a site hook, by the process that
    # holds the refresh token. The import path is the parent's own minus that directory.
    cwd = os.path.realpath(os.getcwd())
    path = [os.path.abspath(p) for p in sys.path if p and os.path.realpath(p) != cwd]
    code = f"import sys\nsys.path[:] = {path!r}\n{WORKER_CODE}"
    return [sys.executable, "-I", "-X", "utf8", "-c", code]


def acquire_generation_credentials(
    connection: GenerationConnection,
    project: str,
    actor: str,
    *,
    deadline: float | None = None,
) -> ActiveCredentials:
    # A caller that already waited for the credential lock shares one budget.
    if deadline is None:
        deadline = renewal_deadline()
    store = connection.store()
    # The whole budget may be spent waiting for another process's renewal; its result is
    # reused below instead of starting a second exchange.
    with store.locked(timeout=_remaining(deadline)):
        record = store.read()
        if record is None or record.user_id != actor or record.state == "logged_out":
            raise AuthenticationError("login_required")
        if (
            not store.incomplete()
            and record.state == "active"
            and record.expires_at > time.time() + RENEWAL_WINDOW_SECONDS
        ):
            return ActiveCredentials(actor, record.access_token)
        if not record.needs_verification() and (store.incomplete() or record.state != "active"):
            raise AuthenticationError("login_required")
        request = RenewalRequest(
            connection=connection,
            project=project,
            actor=actor,
            subject=record.subject,
            revision=record.revision,
            budget=_remaining(deadline),
        )
    try:
        _run_worker(request, _remaining(deadline))
    except ValueError as error:
        # A credential that is still valid outlives a failed renewal attempt.
        try:
            return generation_credentials(connection, actor, lock_timeout=_final_wait(deadline))
        except ValueError:
            raise error from None
    return generation_credentials(connection, actor, lock_timeout=_final_wait(deadline))


def _run_worker(request: RenewalRequest, timeout: float) -> None:
    try:
        result = subprocess.run(
            _worker_command(),
            input=request.model_dump_json(),
            encoding="utf-8",
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker before releasing its caller.
        raise RenewalError(TIMEOUT_MESSAGE) from None
    except OSError:
        raise RenewalError(FAILURE_MESSAGE) from None
    if result.returncode != 0:
        raise RenewalError(FAILURE_MESSAGE)
    try:
        outcome = RenewalOutcome.model_validate_json(result.stdout)
    except ValueError:
        raise RenewalError(FAILURE_MESSAGE) from None
    if outcome.error:
        raise RenewalError(outcome.error)


def renew_requested_credentials(
    request: RenewalRequest,
    client: httpx.Client,
    watchdog: Watchdog | None = None,
    *,
    started: float | None = None,
) -> None:
    # The budget runs on the monotonic clock from the worker's first line, so its whole
    # startup counts and a wall-clock adjustment cannot stretch the exchange past the
    # supervisor's kill. The margin covers the interpreter start before that first line.
    if started is None:
        started = time.monotonic()
    deadline = started + request.budget - EXCHANGE_MARGIN_SECONDS
    auth = GenerationAuth(request.connection, request.project, client)
    store = auth.store

    def needed(record: CredentialRecord | None) -> bool:
        if (
            not isinstance(record, AccountRecord)
            or record.user_id != request.actor
            or record.subject != request.subject
            or record.state == "logged_out"
        ):
            raise AuthenticationError("login_required")
        if (
            not store.incomplete()
            and record.state == "active"
            and record.expires_at > time.time()
            and (
                record.expires_at > time.time() + RENEWAL_WINDOW_SECONDS
                or record.revision != request.revision
            )
        ):
            return False
        if not record.needs_verification() and (store.incomplete() or record.state != "active"):
            raise AuthenticationError("login_required")
        if deadline - time.monotonic() <= 0:
            raise RenewalError(TIMEOUT_MESSAGE)  # nothing has been written yet
        _claim_retry(request)
        if watchdog is not None:
            watchdog.guard(record, deadline)
        return True

    auth.refresh(
        needed=needed, lock_timeout=max(0.1, deadline - time.monotonic()), deadline=deadline
    )


class Watchdog:
    """Restores the saved credentials if the deadline passes before a request was sent.

    HTTP timeouts do not bound name resolution, and the supervisor's kill would leave the
    pending record in place. Fired mid-flight and unsent, it writes the saved record back,
    reports an unsent exchange and ends the process under the lock the sender must take.
    """

    def __init__(self, store: CredentialStore, exit: Callable[[int], Any] = os._exit) -> None:
        self.store, self.exit = store, exit
        self.lock = threading.Lock()
        self.record: CredentialRecord | None = None
        self.deadline = float("inf")
        self.in_flight = self.sent = self.aborting = False

    def guard(self, record: CredentialRecord, deadline: float) -> None:
        self.record, self.deadline = record, deadline
        timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        timer.daemon = True
        timer.start()

    def _expired(self) -> bool:
        # The timer thread can run late; the sending path checks the deadline itself.
        if self.aborting or time.monotonic() >= self.deadline:
            self.aborting = True
        return self.aborting

    @contextlib.contextmanager
    def flight(self) -> Iterator[None]:
        with self.lock:
            if self._expired():
                raise ExchangeNotSentError()
            self.in_flight = True
        try:
            yield
        finally:
            with self.lock:
                self.in_flight = False

    def trace(self, name: str, info: dict[str, Any]) -> None:
        if name.endswith("send_request_headers.started"):
            with self.lock:
                if not self.sent and self._expired():
                    raise ExchangeNotSentError()
                self.sent = True

    def _fire(self) -> None:
        with self.lock:
            if self.sent or self.record is None:
                return
            # Past the deadline nothing may be sent any more. Between requests the main
            # thread restores on its own when the next send is refused; mid-flight it is
            # blocked in network I/O, so the restore happens here and the process ends.
            self.aborting = True
            if not self.in_flight:
                return
            self.store.write(self.record)
            self.store.finish()
            outcome = RenewalOutcome(error=auth_error_message(ExchangeNotSentError()))
            sys.stdout.write(outcome.model_dump_json())
            sys.stdout.flush()
            self.exit(0)


class GuardedClient(httpx.Client):
    """An HTTP client whose every send runs under the watchdog's flight lock."""

    def __init__(self, watchdog: Watchdog, **kwargs: Any) -> None:
        super().__init__(trust_env=False, **kwargs)
        self.watchdog = watchdog

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        request.extensions["trace"] = self.watchdog.trace
        with self.watchdog.flight():
            return super().send(request, **kwargs)


def _claim_retry(request: RenewalRequest) -> None:
    store = request.connection.store()
    path = store.path.with_suffix(".renewal-retry.json")
    try:
        prior = RenewalRetry.model_validate(_private_json(path))
    except FileNotFoundError:
        prior = None
    except ValueError:
        # An unreadable record never blocks renewal: it is replaced below.
        path.unlink(missing_ok=True)
        prior = None
    now = int(time.time())
    if (
        prior is not None
        and prior.actor == request.actor
        and prior.subject == request.subject
        and 0 < prior.retry_after - now <= RETRY_DELAY_SECONDS
    ):
        raise AuthenticationError("renewal_paused")
    retry = RenewalRetry(
        actor=request.actor, subject=request.subject, retry_after=now + RETRY_DELAY_SECONDS
    )
    store.write_private(path, retry.model_dump_json().encode())


def main(started: float | None = None) -> None:
    try:
        request = RenewalRequest.model_validate_json(sys.stdin.read(65537))
        watchdog = Watchdog(request.connection.store())
        with GuardedClient(watchdog) as client:
            renew_requested_credentials(request, client, watchdog, started=started)
        outcome = RenewalOutcome()
    except AUTH_ERRORS as exc:
        outcome = RenewalOutcome(error=auth_error_message(exc))
    sys.stdout.write(outcome.model_dump_json())


if __name__ == "__main__":
    main()
