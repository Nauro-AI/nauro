"""PendingStore class for managing decisions awaiting confirmation.

Provides an in-memory pending store that tracks proposed decisions before
they are committed to the project store. Used by both the CLI extraction
pipeline and the remote MCP server's propose/confirm workflow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from nauro_core.constants import EXPIRY_MINUTES


class PendingStore:
    """In-memory store for pending decision confirmations.

    Pending proposals are lost on process restart — the agent can re-propose
    if needed. Each entry has an auto-generated confirm_id (UUID4) and expires
    after EXPIRY_MINUTES.
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}

    def store(self, proposal: dict, validation_result: dict) -> str:
        """Store a pending proposal and return a confirm_id."""
        self.expire()
        confirm_id = str(uuid.uuid4())
        self._pending[confirm_id] = {
            "proposal": proposal,
            "validation_result": validation_result,
            "created_at": datetime.now(UTC),
        }
        return confirm_id

    def get(self, confirm_id: str) -> dict | None:
        """Retrieve a pending proposal by confirm_id, or None if expired/invalid."""
        self.expire()
        return self._pending.get(confirm_id)

    def remove(self, confirm_id: str) -> None:
        """Remove a confirmed or consumed pending proposal."""
        self._pending.pop(confirm_id, None)

    def expire(self) -> None:
        """Remove entries older than EXPIRY_MINUTES."""
        cutoff = datetime.now(UTC) - timedelta(minutes=EXPIRY_MINUTES)
        expired = [k for k, v in self._pending.items() if v["created_at"] < cutoff]
        for k in expired:
            del self._pending[k]

    def clear_all(self) -> None:
        """Clear all pending proposals (for testing)."""
        self._pending.clear()

    def __len__(self) -> int:
        return len(self._pending)
