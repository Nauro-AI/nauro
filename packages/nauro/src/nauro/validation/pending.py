"""Pending confirmation store for two-way MCP writes.

Module-level function API preserved while the confirm path still calls
module-level helpers; the actual storage delegates to the kernel-owned
:class:`~nauro_core.pending.PendingStore` so the validated
``propose_decision`` and ``confirm_decision`` paths share one singleton.
Retires once the confirm path moves into the kernel directly.
"""

from __future__ import annotations

from nauro_core.operations.propose_decision import _get_pending_store

_store = _get_pending_store()


def store_pending(proposal: dict, validation_result: dict) -> str:
    """Store a pending proposal and return a confirm_id."""
    return _store.store(proposal, validation_result)


def get_pending(confirm_id: str) -> dict | None:
    """Retrieve a pending proposal by confirm_id, or None if expired/invalid."""
    return _store.get(confirm_id)


def remove_pending(confirm_id: str) -> None:
    """Remove a confirmed or consumed pending proposal."""
    _store.remove(confirm_id)


def expire_pending() -> None:
    """Remove entries older than EXPIRY_MINUTES."""
    _store.expire()


def clear_all() -> None:
    """Clear all pending proposals (for testing)."""
    _store.clear_all()
