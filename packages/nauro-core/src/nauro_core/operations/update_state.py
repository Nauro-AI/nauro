"""``update_state`` — replace current state with a new delta.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`UpdateStateResult`. The kernel handles the read/migrate/write
plumbing through the :class:`~nauro_core.operations.store.Store`
protocol; snapshot capture, cloud-sync push, and length
validation stay on the adapter side.

The kernel uses the pure-function helpers in :mod:`nauro_core.state`
(``prepare_state_update`` / ``migrate_legacy_state``) so the on-disk
format is identical across surfaces.
"""

from __future__ import annotations

from nauro_core.constants import (
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_LEGACY_FILENAME,
    STATE_OVERLAP_MIN_KEYWORDS,
    STATE_OVERLAP_STOP_WORDS,
)
from nauro_core.operations.results import UpdateStateResult
from nauro_core.operations.store import Store
from nauro_core.state import migrate_legacy_state, prepare_state_update


def update_state(store: Store, delta: str) -> UpdateStateResult:
    """Replace current state with *delta*, archiving the prior body.

    Args:
        store: Storage adapter. The kernel reads ``state_current.md`` and
            falls back to legacy ``state.md`` via :meth:`Store.read_file`;
            both writes go through :meth:`Store.write_file`.
        delta: New state body. Length validation lives on the adapter
            side — the kernel writes whatever the adapter passes through.

    Returns:
        :class:`UpdateStateResult`. ``status="ok"`` on a successful write,
        ``status="noop"`` when the store has no existing state file
        (the kernel early-returns without writing). ``warning``
        carries a keyword-overlap caution when the delta heavily mirrors
        an existing bullet in ``state_current.md``.
    """
    current_content = store.read_file(STATE_CURRENT_FILENAME)
    using_legacy = False
    if current_content is None:
        legacy_content = store.read_file(STATE_LEGACY_FILENAME)
        if legacy_content is None:
            return UpdateStateResult(status="noop")
        using_legacy = True
        migrated = migrate_legacy_state(legacy_content)
        store.write_file(STATE_CURRENT_FILENAME, migrated.current_content)
        current_content = migrated.current_content

    warning = _overlap_warning(delta, current_content) if not using_legacy else None

    result = prepare_state_update(delta, current_content)
    store.write_file(STATE_CURRENT_FILENAME, result.current_content)

    if result.history_entry is not None:
        existing_history = store.read_file(STATE_HISTORY_FILENAME) or ""
        store.write_file(STATE_HISTORY_FILENAME, existing_history + result.history_entry)

    return UpdateStateResult(status="ok", warning=warning)


def _overlap_warning(delta: str, current_content: str) -> str | None:
    """Return a caution string when *delta* heavily mirrors a current entry.

    Scans the current state body for bullet lines and flags the first one
    that shares :data:`STATE_OVERLAP_MIN_KEYWORDS` or more non-stop-word
    tokens with *delta*. Returns ``None`` when no qualifying overlap is
    found.
    """
    delta_words = set(delta.lower().split())
    for line in current_content.split("\n"):
        if not line.startswith("- ") or "none yet" in line:
            continue
        line_words = set(line.lower().split())
        overlap = delta_words & line_words - STATE_OVERLAP_STOP_WORDS
        if len(overlap) >= STATE_OVERLAP_MIN_KEYWORDS:
            return f"State update shares keywords with existing entry: {line.strip()}"
    return None
