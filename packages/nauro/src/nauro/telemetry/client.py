"""Lazy PostHog client singleton.

Project key resolution: the NAURO_POSTHOG_KEY env var wins when set;
otherwise the baked-in ``_BAKED_PROJECT_KEY`` constant is used, unless it is
still the self-disabling release placeholder (in which case the key resolves
to None and telemetry stays off).

No atexit handler and no posthog.shutdown() call: the CLI uses
sync_mode=True, so each capture() blocks until sent and there is no daemon
thread to flush. Adding shutdown would re-introduce a background-thread
surface in a short-lived process the user is watching exit.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

POSTHOG_KEY_ENV = "NAURO_POSTHOG_KEY"
POSTHOG_HOST = "https://us.i.posthog.com"

# Baked-in PostHog project API key, shipped in the published wheel by design.
# This is an intentionally-public, WRITE-ONLY ingestion key (PostHog phc_ keys
# can only submit events, never read them) — see PRIVACY.md for the full
# data-collection contract. The NAURO_POSTHOG_KEY env var still overrides it.
# Emission stays gated by consent (_should_emit): off by default, enabled only
# on an explicit interactive first-run opt-in, so CI and non-TTY installs never
# emit regardless of this key. The "phc_REPLACE" guard in _resolve_project_key()
# is a safety net:
# if this value is ever reset to a placeholder, telemetry disables cleanly.
_BAKED_PROJECT_KEY = "phc_oGL7Q29uiGrocGujHP5TvJzmPfJLKoej7BKsQRL5S35J"

_RESERVED_PREFIXES = ("$ip", "$geoip_", "$user_agent")

_client: Any = None
_client_lock = threading.Lock()


def _resolve_project_key() -> str | None:
    key = os.environ.get(POSTHOG_KEY_ENV)
    if key:
        return key
    if not _BAKED_PROJECT_KEY or _BAKED_PROJECT_KEY.startswith("phc_REPLACE"):
        return None
    return _BAKED_PROJECT_KEY


def _strip_reserved(properties: dict[str, Any]) -> dict[str, Any]:
    """Drop any property whose key starts with $ip, $geoip_, or $user_agent.

    Defense-in-depth: PostHog v7 has paths that can re-add these even with
    disable_geoip=True; this filter is the runtime guarantee that the
    privacy contract holds across SDK upgrades.
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

    Returns None when no project key resolves (env var unset and the baked-in
    key is still the release placeholder) — callers must treat that as
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

        client = Posthog(
            project_api_key=key,
            host=POSTHOG_HOST,
            sync_mode=True,
            disable_geoip=True,
            enable_exception_autocapture=False,
            before_send=_before_send,
        )
        # posthog swallows transport errors internally and logs them at ERROR via
        # the "posthog" logger, so capture()'s try/except never sees them and a
        # network failure would traceback to stderr. Ordering is load-bearing:
        # AFTER Posthog() (its __init__ resets this logger to WARNING) and BEFORE
        # publishing _client (so no thread emits through the still-WARNING logger).
        logging.getLogger("posthog").setLevel(logging.CRITICAL)
        _client = client
    return _client
