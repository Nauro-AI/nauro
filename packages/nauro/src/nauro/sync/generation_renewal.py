"""Bounded automatic credential acquisition before generation operations."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
from pydantic import BaseModel, ConfigDict

from nauro.auth import ActiveCredentials
from nauro.sync.auth_errors import AuthenticationError, auth_error_message
from nauro.sync.decision_profile import _private_json
from nauro.sync.generation_credentials import (
    AccountRecord,
    GenerationAuth,
    GenerationConnection,
    generation_credentials,
)
from nauro.sync.reference_auth import AUTH_ERRORS
from nauro.sync.reference_credentials import CredentialRecord

RENEWAL_WINDOW_SECONDS = 60
RENEWAL_TIMEOUT_SECONDS = 8.0
RETRY_DELAY_SECONDS = 30
# The worker stops its own network work this long before the supervisor's kill. A
# request that never connects restores the saved credentials; once a request is sent,
# a stalled response is an uncertain exchange and clears them like any other failure.
EXCHANGE_MARGIN_SECONDS = 1.5
TIMEOUT_MESSAGE = "Credential renewal timed out. Run 'nauro auth refresh' to recover."
FAILURE_MESSAGE = "Credential renewal failed. Run 'nauro auth refresh' to recover."
WORKER_CODE = "from nauro.sync.generation_renewal import main\nmain()\n"


class RenewalError(ValueError):
    """Automatic renewal produced no usable credentials; the message names the recovery."""


class RenewalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    connection: GenerationConnection
    project: str
    actor: str
    subject: str
    revision: str
    deadline: float  # wall-clock time shared with the worker; it stops before the kill


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
            deadline=time.time() + _remaining(deadline),
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


def renew_requested_credentials(request: RenewalRequest, client: httpx.Client) -> None:
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
        if request.deadline - time.time() < EXCHANGE_MARGIN_SECONDS:
            raise RenewalError(TIMEOUT_MESSAGE)  # nothing has been written yet
        _claim_retry(request)
        return True

    auth.refresh(
        needed=needed,
        lock_timeout=max(0.1, request.deadline - time.time() - EXCHANGE_MARGIN_SECONDS),
        deadline=request.deadline - EXCHANGE_MARGIN_SECONDS,
    )


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


def main() -> None:
    try:
        request = RenewalRequest.model_validate_json(sys.stdin.read(65537))
        with httpx.Client(trust_env=False) as client:
            renew_requested_credentials(request, client)
        outcome = RenewalOutcome()
    except AUTH_ERRORS as exc:
        outcome = RenewalOutcome(error=auth_error_message(exc))
    sys.stdout.write(outcome.model_dump_json())


if __name__ == "__main__":
    main()
