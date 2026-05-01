"""First-run telemetry consent prompt.

Idempotent across CLI invocations: once cfg.consent_version matches the current
TELEMETRY_CONSENT_VERSION the prompt is a no-op. Bumping the constant
re-triggers the prompt with the user's previous answer pre-selected as default,
so an unchanged-mind user just presses Enter.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from nauro.constants import NAURO_TELEMETRY_ENV, TELEMETRY_CONSENT_VERSION
from nauro.store.config import get_telemetry_config, load_config, save_config

PRIVACY_URL = "https://github.com/Nauro-AI/nauro/blob/main/packages/nauro/PRIVACY.md"
PROMPT_TEXT = f"Help improve Nauro? Anonymous usage data only. See {PRIVACY_URL}"
REPROMPT_PREFACE = (
    "Telemetry events have expanded since you last consented. See PRIVACY.md for what's new."
)


def _persist(enabled: bool) -> None:
    data = load_config()
    section = data.get("telemetry") or {}
    section["enabled"] = enabled
    section["consent_version"] = TELEMETRY_CONSENT_VERSION
    section["consented_at"] = datetime.now(UTC).isoformat()
    data["telemetry"] = section
    save_config(data)


def _parse_answer(raw: str, default_yes: bool) -> bool:
    answer = raw.strip().lower()
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    if answer == "":
        return default_yes
    # Unknown input → opposite of default per the behavior matrix.
    return not default_yes


def maybe_prompt() -> None:
    """Run the first-run / version-bump consent prompt if needed."""
    if os.environ.get(NAURO_TELEMETRY_ENV) == "0":
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Both streams must be a TTY. The post-commit hook backgrounds `nauro extract`
        # with stdout/stderr redirected (`> /dev/null 2>&1 &`) but inherits stdin from
        # the user's terminal — checking stdin alone passes the gate, then input() in a
        # background process triggers SIGTTIN and leaves the extract job suspended.
        # Skipping here also avoids generating the anonymous_id on non-interactive runs.
        return

    cfg = get_telemetry_config()
    if cfg.consent_version == TELEMETRY_CONSENT_VERSION:
        return

    is_first_run = cfg.consent_version is None
    if is_first_run:
        default_yes = True
    else:
        default_yes = cfg.enabled is True
        print(REPROMPT_PREFACE)

    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw = input(f"{PROMPT_TEXT} {suffix} ")
    enabled = _parse_answer(raw, default_yes)
    _persist(enabled)
