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
    """Phase 1a: anonymous_id only. Phase 1c extends to user_id when logged in (D119)."""
    cfg = get_telemetry_config()
    return cfg.anonymous_id


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


def identify_login(user_id: str) -> None:
    raise NotImplementedError("Implemented in Phase 1c per D119")


def identify_logout() -> None:
    raise NotImplementedError("Implemented in Phase 1c per D119")
