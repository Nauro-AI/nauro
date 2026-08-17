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

    All implementations are synchronous; cloud transports wrap operation calls in
    ``asyncio.to_thread`` or equivalent. Path traversal, locking, and
    backend-specific error mapping are the implementation's own boundary.
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
        """Return decision file stems (e.g. ``"042-use-postgres"``) in lexicographic order.

        Lexicographic is not chronological: order through ``sort_stems_by_number`` first.
        """
        ...

    def read_decision(self, file_stem: str) -> str | None:
        """Return the markdown body for the decision named by ``file_stem``.

        A stem from :meth:`list_decisions` without ``.md``; ``None`` when it is missing.
        """
        ...

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        """Bulk analogue of :meth:`read_decision`, returning ``{stem: body | None}``.
        There is NO ordering guarantee (transports may fan the reads out), so a caller
        needing a stable order reasserts it against the ``stems`` list it passed.
        """
        ...
