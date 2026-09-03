"""Event-driven sync hooks: pull on session start, push after writes.

Called by the MCP server (``stdio_server._pull_on_startup`` on entry,
``mcp/tools._try_push`` after each write). Neither hook blocks or crashes:
failures are logged, and a post-write report returns to the post-commit surface.

Both gate on Auth0 token presence and v2 cloud-mode at entry and silently no-op
when either is missing. An unauthenticated user is not nagged on every tool call,
and a project with no cloud-mode registry record has no presign target.

The transport lives in ``nauro.sync.pull`` and ``nauro.sync.push``, shared with
the ``nauro sync`` CLI command; these hooks supply a logging ``Reporter`` so the
shared core stays silent here while the CLI echoes progress. Token refresh on 401
happens inside the transport, and ``AuthRefreshError`` is swallowed as a log line.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from nauro.auth import load_access_token
from nauro.store.registry import is_cloud_project

if TYPE_CHECKING:
    from nauro.sync.push import PushReport

logger = logging.getLogger("nauro.sync")


class _LoggingReporter:
    """Pull reporter for the SessionStart hook.

    Routes progress to ``logger`` at the appropriate level so auto-pull
    never writes to the session's terminal.
    """

    def info(self, msg: str) -> None:
        logger.info("sync pull: %s", msg)

    def warn(self, msg: str) -> None:
        logger.warning("sync pull: %s", msg)


def pull_before_session(project_id: str, store_path: Path) -> int:
    """Pull remote changes before a session starts, returning the file count.

    Silent no-op (0) when not authenticated or not v2 cloud-mode; never raises.
    """
    if not load_access_token():
        return 0
    if not is_cloud_project(project_id):
        return 0

    from nauro.sync.lock import HOOK_SYNC_LOCK_TIMEOUT, SyncLockTimeoutError
    from nauro.sync.pull import run_pull

    try:
        # The merged count only: session start has no exit code to carry a
        # refusal, and the reporter above has already logged every one.
        return run_pull(
            project_id,
            store_path,
            _LoggingReporter(),
            lock_timeout=HOOK_SYNC_LOCK_TIMEOUT,
        ).merged
    except SyncLockTimeoutError as exc:
        # Contention is an ordinary outcome here, not a failure: the other
        # process is doing this same work, and session start never waits.
        logger.info("sync pull: skipped, %s", exc)
        return 0
    except Exception:
        # A genuinely unexpected error must not escape session startup.
        logger.exception("sync pull: unexpected failure for %s", project_id)
        return 0


def push_after_write(project_id: str, store_path: Path) -> PushReport:
    """Push changed local files after a write, returning a structured report.

    Silent no-op when not authenticated or not v2 cloud-mode; never raises.
    """
    from nauro.sync.push import PushReport

    if not load_access_token():
        return PushReport()
    if not is_cloud_project(project_id):
        return PushReport()

    from nauro.auth import AuthRefreshError
    from nauro.sync._path_diagnostics import _StoreRootPreparationError
    from nauro.sync.lock import HOOK_SYNC_LOCK_TIMEOUT, SyncLockTimeoutError
    from nauro.sync.push import push_changed_files
    from nauro.sync.remote import PresignError

    try:
        return push_changed_files(project_id, store_path, lock_timeout=HOOK_SYNC_LOCK_TIMEOUT)
    except SyncLockTimeoutError as exc:
        # Post-write publication is a fail-open ancillary step: the changed
        # files stay changed and the next push picks them up.
        logger.info("sync push: skipped, %s", exc)
        return PushReport(failed=("sync lock",))
    except AuthRefreshError as exc:
        logger.warning("sync push: %s", exc)
        return PushReport(failed=("authentication",))
    except PresignError as exc:
        logger.warning("sync push: presign request failed: %s", exc)
        return PushReport(failed=("transport",))
    except _StoreRootPreparationError as exc:
        logger.warning("sync push: %s", exc)
        return PushReport(failed=("store root",))
    except Exception:
        logger.exception("sync push: unexpected failure for %s", project_id)
        return PushReport(failed=("unexpected failure",))
