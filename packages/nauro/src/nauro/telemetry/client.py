"""Lazy PostHog client singleton.

Project key is read from NAURO_POSTHOG_KEY env var only in this PR — T4.1 will
flip the default to an embedded prod key. Single-PR ownership keeps the
"placeholder accidentally shipped" failure mode out of Phases 1a–1c.

No atexit handler and no posthog.shutdown() call anywhere: per D120 the CLI
uses sync_mode=True, so each capture() blocks until sent — there is no daemon
thread that needs flushing on exit. Adding shutdown would re-introduce the
background-thread surface that feedback_daemon_removed exists to prevent.
"""

from __future__ import annotations

import os
import threading
from typing import Any

POSTHOG_KEY_ENV = "NAURO_POSTHOG_KEY"
POSTHOG_HOST = "https://us.i.posthog.com"

_RESERVED_PREFIXES = ("$ip", "$geoip_", "$user_agent")

_client: Any = None
_client_lock = threading.Lock()


def _resolve_project_key() -> str | None:
    key = os.environ.get(POSTHOG_KEY_ENV)
    return key or None


def _strip_reserved(properties: dict[str, Any]) -> dict[str, Any]:
    """Drop any property whose key starts with $ip, $geoip_, or $user_agent.

    Defense-in-depth per D120: PostHog v7 has paths that can re-add these
    properties even with disable_geoip=True; this filter is the runtime
    guarantee that the privacy contract holds across SDK upgrades.
    """
    return {
        k: v
        for k, v in properties.items()
        if not any(k.startswith(prefix) for prefix in _RESERVED_PREFIXES)
    }


def _before_send(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return event
    props = event.get("properties")
    if isinstance(props, dict):
        event["properties"] = _strip_reserved(props)
    return event


def get_client() -> Any | None:
    """Return the singleton PostHog client, initializing it on first call.

    Returns None when NAURO_POSTHOG_KEY is unset — callers must treat that as
    "telemetry disabled" and no-op.
    """
    global _client
    if _client is not None:
        return _client

    key = _resolve_project_key()
    if key is None:
        return None

    with _client_lock:
        if _client is not None:
            return _client
        from posthog import Posthog

        _client = Posthog(
            project_api_key=key,
            host=POSTHOG_HOST,
            sync_mode=True,
            disable_geoip=True,
            enable_exception_autocapture=False,
            before_send=_before_send,
        )
    return _client
