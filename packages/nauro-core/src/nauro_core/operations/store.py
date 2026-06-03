"""Storage adapter protocol for the operations kernel.

Operations call into a ``Store`` to read and write the project store; each
transport supplies a concrete implementation (filesystem for local, S3 +
DynamoDB for cloud). The Protocol stays minimal: the six primitives locked
by the operations-kernel restructure and the bulk-read addition. Anything
broader (file enumeration outside ``decisions/``, pending-state primitives,
etc.) is a separate decision before the surface grows.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Minimal storage interface the operations kernel depends on.

    All implementations are synchronous; cloud transports wrap operation
    calls in ``asyncio.to_thread`` (or equivalent) so the kernel itself
    stays free of infrastructure concerns. Implementations are expected to
    handle path traversal, locking, and any backend-specific error mapping
    at their own boundary.
    """

    def read_file(self, path: str) -> str | None:
        """Return the file's text content, or ``None`` if it does not exist."""
        ...

    def write_file(self, path: str, content: str) -> None:
        """Write ``content`` to ``path``, replacing any existing content."""
        ...

    def delete_file(self, path: str) -> None:
        """Remove ``path``. No-op if the file does not exist."""
        ...

    def list_decisions(self) -> list[str]:
        """Return decision file stems (e.g. ``"042-use-postgres"``).

        The list is sorted in lexicographic order. Stems map 1:1 to decision
        files under the canonical ``decisions/`` directory; callers reach
        the body via :meth:`read_decision`.
        """
        ...

    def read_decision(self, file_stem: str) -> str | None:
        """Return the markdown body for the decision named by ``file_stem``.

        ``file_stem`` is a value returned by :meth:`list_decisions` (without
        the ``.md`` suffix). Returns ``None`` if the decision is missing.
        """
        ...

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        """Bulk analogue of :meth:`read_decision`.

        Returns ``{stem: body | None}`` for each stem in ``stems``; a stem
        whose file is missing maps to ``None``. There is NO ordering
        guarantee on the returned mapping — callers that need a stable order
        reassert it against the ``stems`` list they passed. Cloud transports
        may fan the reads out concurrently, so the mapping's iteration order
        is not the call order.
        """
        ...
