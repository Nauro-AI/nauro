"""Retry policy for presigned object downloads.

A restore pulls a whole record over one network, so a single dropped
connection must not cost the run. What counts as retryable, how long to wait,
and when a presigned URL is stale are one judgment, so they live here rather
than at the call sites: the store layer decides what to do with the bytes, and
this module decides whether there are bytes to have.

Classification takes the same posture as the server-side storage classifier: a
fault we cannot name is a fault we cannot promise a retry will fix, so
anything unrecognized is permanent.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol

from nauro.sync.remote import PresignError, PresignTransferError

# Object storage and the API edge answer overload and their own faults with
# these; every other status names something the same request will not fix.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# A presigned URL past its TTL answers 403, and so does a URL the caller was
# never entitled to. The response cannot tell them apart, so the first 403
# buys one fresh mint and a second 403 settles which one it was.
_EXPIRED_STATUS = 403

# Four attempts spans a few seconds of packet loss without holding a restore
# open against a network that is simply down.
_MAX_ATTEMPTS = 4
_BASE_DELAY_SECONDS = 0.5
_MAX_DELAY_SECONDS = 8.0


class TransferFault(Enum):
    """What a failed download says about trying again."""

    TRANSIENT = "transient"
    EXPIRED_CANDIDATE = "expired-candidate"
    PERMANENT = "permanent"


class Reporter(Protocol):
    """Surface for transfer progress, shared by every path that moves bytes.

    The pull core and the cloud restore both report through it, and the CLI,
    the hooks, and the tests each supply their own implementation: the CLI
    echoes to the terminal, the hook logs quietly (session startup must never
    crash), and a caller that surfaces nothing takes :class:`NullReporter`.
    """

    def info(self, msg: str) -> None:
        """Report routine progress (a file written, a count, a completion)."""

    def warn(self, msg: str) -> None:
        """Report a recoverable anomaly (a URL shortfall, a kept partial run)."""


class NullReporter:
    """Reporter for callers that surface nothing (library use, tests)."""

    def info(self, msg: str) -> None:
        """Discard routine progress."""

    def warn(self, msg: str) -> None:
        """Discard anomaly reports."""


class UrlSource(Protocol):
    """The presigned URLs for one batch, re-mintable while the batch drains."""

    def url_for(self, path: str) -> str:
        """Return the current download URL for ``path``."""

    def remint(self) -> None:
        """Mint fresh URLs for every path in the batch still outstanding."""


def classify_fault(error: PresignError) -> TransferFault:
    """Judge whether a failed download is worth another attempt."""
    if not isinstance(error, PresignTransferError):
        return TransferFault.PERMANENT
    if error.status is None:
        return TransferFault.TRANSIENT if error.transport else TransferFault.PERMANENT
    if error.status in _TRANSIENT_STATUSES:
        return TransferFault.TRANSIENT
    if error.status == _EXPIRED_STATUS:
        return TransferFault.EXPIRED_CANDIDATE
    return TransferFault.PERMANENT


def backoff_delay(failures: int) -> float:
    """Return the wait before the retry that follows ``failures`` failures.

    Full jitter, not a fixed exponential step: a whole record's files fail
    together when a network drops, and equal waits would retry them in
    lockstep against the endpoint that just refused them.
    """
    ceiling = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * 2 ** (failures - 1))
    return random.uniform(0.0, ceiling)


def pause(seconds: float) -> None:
    """Wait between attempts. A seam: tests replace it to run at full speed."""
    time.sleep(seconds)


def download_with_retry(path: str, urls: UrlSource, fetch: Callable[[str], bytes]) -> bytes:
    """Download one object, retrying transient faults and one stale URL.

    A re-mint is a credential refresh, not a network fault, so it resets the
    transient budget instead of spending from it: the URL that failed and the
    URL that replaces it are different requests, and the second one has to be
    able to ride out a dropped connection too. The total stays bounded because
    ``reminted`` is a one-time latch - at worst a full budget before the mint
    and a full budget after it, then the second refusal falls through as
    permanent.

    Raises:
        ~nauro.sync.remote.PresignError: the last failure, once the fault is
            permanent or the attempt budget is spent.
    """
    failures = 0
    reminted = False
    while True:
        try:
            return fetch(urls.url_for(path))
        except PresignError as exc:
            failures += 1
            fault = classify_fault(exc)
            if fault is TransferFault.EXPIRED_CANDIDATE and not reminted:
                # A URL minted seconds ago that still answers 403 is refused,
                # not expired, so the mint is offered exactly once.
                reminted = True
                failures = 0
                urls.remint()
                continue
            if fault is not TransferFault.TRANSIENT or failures >= _MAX_ATTEMPTS:
                raise
            pause(backoff_delay(failures))


__all__ = [
    "NullReporter",
    "Reporter",
    "TransferFault",
    "UrlSource",
    "backoff_delay",
    "classify_fault",
    "download_with_retry",
    "pause",
]
