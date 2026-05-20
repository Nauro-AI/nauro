"""Dict-backed ``Store`` implementation for kernel tests.

Keeps the test surface free of filesystem I/O. Decision file stems live in
their own dict so :meth:`list_decisions` can return a sorted view without
introspecting the broader file table.
"""

from __future__ import annotations


class InMemoryStore:
    """Test double for :class:`~nauro_core.operations.store.Store`.

    Decisions are passed in keyed by file stem and exposed via
    :meth:`list_decisions` / :meth:`read_decision`. Other files live in a
    separate dict so write/read/delete round-trip without polluting the
    decision view.
    """

    def __init__(
        self,
        decisions: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        self._decisions: dict[str, str] = dict(decisions or {})
        self._files: dict[str, str] = dict(files or {})

    def read_file(self, path: str) -> str | None:
        return self._files.get(path)

    def write_file(self, path: str, content: str) -> None:
        self._files[path] = content

    def delete_file(self, path: str) -> None:
        self._files.pop(path, None)

    def list_decisions(self) -> list[str]:
        return sorted(self._decisions)

    def read_decision(self, file_stem: str) -> str | None:
        return self._decisions.get(file_stem)
