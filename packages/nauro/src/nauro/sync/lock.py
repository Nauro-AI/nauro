"""Store-scoped lock serializing sync pull and push.

The pull core classifies a colliding decision, renames the local file, rewrites
its heading, updates the hash index and installs the remote file as separate
filesystem steps, so a concurrent pull or push seeing the store mid-sequence
would classify against a half-applied state. Both directions take one lock per
store for the whole run, and every acquisition carries a finite timeout: an
explicit ``nauro sync`` fails loud, and the SessionStart hook skips its pull.

Lock order is one-way: this lock is taken outside ``decision_write_lock``, never
inside it, so a sync holding the store can still mint a decision number while a
kernel writer taking only the decision lock never blocks on a sync. The lock file
sits beside ``.sync-state.json`` and ends in ``.json.lock``, so ``should_skip``
already excludes it from the push scan and the manifest walk.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from nauro.constants import DECISIONS_DIR
from nauro.store.store_lock import rmw_lock_path
from nauro.sync.state import SYNC_STATE_FILE

SYNC_LOCK_FILE = SYNC_STATE_FILE + ".lock"

# An explicit `nauro sync` waits out a short overlapping sync (a SessionStart
# pull, an MCP post-write push) rather than failing on a routine race.
CLI_SYNC_LOCK_TIMEOUT = 10.0

# Background paths never wait: session startup must not stall behind another
# process's transfer, and a post-write push is a fail-open ancillary step.
HOOK_SYNC_LOCK_TIMEOUT = 1.0


class SyncLockTimeoutError(Exception):
    """A lock a sync needs was held by someone else past the timeout."""

    def __init__(self, store_path: Path, timeout: float, resource: str = "store") -> None:
        super().__init__(
            f"another writer is busy with {store_path.name}; "
            f"waited {timeout:g}s for the {resource} lock"
        )
        self.store_path = store_path
        self.timeout = timeout
        self.resource = resource


@contextmanager
def _bounded(lock_path: Path, store_path: Path, timeout: float, resource: str):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as exc:
        raise SyncLockTimeoutError(store_path, timeout, resource) from exc
    try:
        yield
    finally:
        lock.release()


@contextmanager
def sync_lock(store_path: Path, timeout: float):
    """Hold the store's sync lock for the duration of the block.

    Raises ``SyncLockTimeoutError`` when it is not acquired within ``timeout``.
    """
    with _bounded(store_path / SYNC_LOCK_FILE, store_path, timeout, "store"):
        yield


@contextmanager
def decision_lock(store_path: Path, timeout: float):
    """Hold the decision allocation lock, raising ``SyncLockTimeoutError`` on timeout.

    Same file as ``decision_write_lock``, inside the sync lock, never around network IO.
    """
    with _bounded(
        rmw_lock_path(store_path, DECISIONS_DIR, is_directory=True),
        store_path,
        timeout,
        "decisions",
    ):
        yield


__all__ = [
    "CLI_SYNC_LOCK_TIMEOUT",
    "HOOK_SYNC_LOCK_TIMEOUT",
    "SYNC_LOCK_FILE",
    "SyncLockTimeoutError",
    "decision_lock",
    "sync_lock",
]
