"""Event-driven sync hooks — pull on session start, push after writes.

Called by the MCP server (``stdio_server._pull_on_startup`` on entry,
``mcp/tools._try_push`` after each write). Never block or crash —
failures are logged only.

Both hooks gate on Auth0 token presence and v2 cloud-mode at entry and
silent-no-op when either is missing. The two no-op cases are:

* Not authenticated. MCP writes happen on every tool call; nagging
  ``run nauro auth login`` on every write would be hostile. The user
  saw the prompt at session start (or onboarding) — here we just skip.
* Project is not v2 cloud-mode. v1 entries have no server-side ULID
  and v2 local-mode is not remote-backed. The presign endpoints can
  address neither.

The pull and push transport lives in ``nauro.sync.pull`` and
``nauro.sync.push`` and is shared with the ``nauro sync`` CLI command.
These hooks supply a logging :class:`~nauro.sync.pull.Reporter`, so the
shared core stays silent and crash-free here while the CLI echoes
progress to the terminal.

Token refresh on 401 is handled inside ``request_presigned_urls`` and
``fetch_manifest`` via ``with_token_refresh``. ``AuthRefreshError``
escapes here as a swallowed log line.
"""

import logging
from pathlib import Path

from nauro.cli.commands.auth import load_access_token
from nauro.store.registry import is_cloud_project

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
    """Pull remote changes from the server before a session starts.

    Silent no-op when not authenticated or when ``project_id`` is not a
    v2 cloud-mode entry. Returns the number of files pulled/merged, or
    0 on any swallowed failure. Never raises — auto-pull must not crash
    session startup.
    """
    if not load_access_token():
        return 0
    if not is_cloud_project(project_id):
        return 0

    try:
        from nauro.sync.pull import run_pull
    except ImportError:
        return 0

    try:
        return run_pull(project_id, store_path, _LoggingReporter())
    except Exception:
        # A genuinely unexpected error must not escape session startup.
        logger.exception("sync pull: unexpected failure for %s", project_id)
        return 0


def push_after_write(project_id: str, store_path: Path) -> int:
    """Push changed local files after a write (decision, question, state).

    Silent no-op when not authenticated or when ``project_id`` is not a
    v2 cloud-mode entry. Returns the number of files pushed, or 0 on any
    swallowed failure. Never raises — auto-push must not surface errors
    on every MCP tool call.
    """
    if not load_access_token():
        return 0
    if not is_cloud_project(project_id):
        return 0

    try:
        from nauro.cli.commands.auth import AuthRefreshError
        from nauro.sync.push import push_changed_files
        from nauro.sync.remote import PresignError
    except ImportError:
        return 0

    try:
        return push_changed_files(project_id, store_path)
    except AuthRefreshError as exc:
        logger.warning("sync push: %s", exc)
        return 0
    except PresignError as exc:
        logger.warning("sync push: presign request failed: %s", exc)
        return 0
    except Exception:
        logger.exception("sync push: unexpected failure for %s", project_id)
        return 0
