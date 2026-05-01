"""Public telemetry API.

Phase 1a: capture() is wired but emits nothing yet (no call sites). Phases 1b/1c
add the call sites. identify_login/identify_logout are deliberately stubs that
raise NotImplementedError — silent no-ops would let Phase 1b accidentally depend
on identity work and silently corrupt analytics.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from nauro.constants import NAURO_TELEMETRY_ENV
from nauro.store.config import get_telemetry_config, load_config, save_config
from nauro.telemetry.client import _resolve_project_key, get_client

logger = logging.getLogger("nauro.telemetry")


def _should_emit() -> bool:
    """Three guards: opt-out env, missing PostHog key, fresh-install / opt-out consent.

    The anonymous_id-set check from D117 is satisfied by get_telemetry_config()'s
    contract: it generates and persists a UUID4 on first read, so cfg.anonymous_id
    is never None by construction.
    """
    if os.environ.get(NAURO_TELEMETRY_ENV) == "0":
        return False
    if _resolve_project_key() is None:
        return False
    cfg = get_telemetry_config()
    if cfg.enabled is not True:
        return False
    return True


def _get_distinct_id() -> str:
    """Resolve the PostHog distinct_id for this process.

    Phase 1c (D119): prefer ``config.auth.user_id`` if logged in; otherwise the
    anonymous_id from the telemetry section. The alias call in identify_login
    ties pre-login anonymous events to the user_id post-login.
    """
    auth = load_config().get("auth") or {}
    user_id = auth.get("user_id")
    if user_id:
        return user_id
    return get_telemetry_config().anonymous_id


def is_enabled() -> bool:
    return _should_emit()


def capture(event_name: str, properties: dict[str, Any] | None = None) -> None:
    """Send an event if telemetry is enabled. Silent on failure — must never crash the CLI."""
    if not _should_emit():
        return
    try:
        client = get_client()
        if client is None:
            return
        client.capture(
            event=event_name,
            distinct_id=_get_distinct_id(),
            properties=properties or {},
        )
    except Exception:
        logger.debug("telemetry capture failed", exc_info=True)


def _rotate_anonymous_id() -> str:
    """Mint a fresh UUID4 anonymous_id, persist it, leave consent fields untouched.

    Used by ``nauro telemetry reset`` and (in Phase 1c) by identify_logout(). The
    rotation is application-side only — PostHog's Python SDK is stateless w.r.t.
    identity, so subsequent capture() calls will pick up the new id via
    _get_distinct_id() without any SDK reset.
    """
    new_id = str(uuid.uuid4())
    data = load_config()
    section = data.get("telemetry") or {}
    section["anonymous_id"] = new_id
    data["telemetry"] = section
    save_config(data)
    return new_id


def identify_login(user_id: str, email_hash: str) -> None:
    """Merge anonymous identity into user_id on login (D119).

    Order is load-bearing: alias(previous_id=anonymous_id, distinct_id=user_id)
    FIRST, then set(distinct_id=user_id, properties={"email_hash"}). Reversed
    alias args alias the identified user back to the anonymous id and split
    future events across two distinct_ids in PostHog — silently corrupts
    analytics. Auth state (config.auth.user_id) is persisted by auth.py
    BEFORE this call, so identify_login is purely the telemetry side.

    Guarded by _should_emit() — telemetry-disabled callers no-op.
    """
    if not _should_emit():
        return
    try:
        client = get_client()
        if client is None:
            return
        cfg = get_telemetry_config()
        client.alias(previous_id=cfg.anonymous_id, distinct_id=user_id)
        client.set(distinct_id=user_id, properties={"email_hash": email_hash})
    except Exception:
        logger.debug("identify_login failed", exc_info=True)


def identify_logout() -> None:
    """Rotate anonymous_id on logout (D119) — no SDK reset.

    posthog.reset() is a JS-SDK API; the Python SDK 7.x has no such method
    (every capture() takes an explicit distinct_id, so identity is stateless
    on the SDK side). Identity reset is application-side rotation only —
    the next capture() re-resolves through _get_distinct_id() which now
    returns the new anonymous_id (because config.auth.user_id is gone).

    auth.py clears config.auth AFTER this call. Consent fields
    (enabled, consent_version, consented_at) are preserved by
    _rotate_anonymous_id().
    """
    _rotate_anonymous_id()
