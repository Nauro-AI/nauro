"""Dict-backed ``Store`` implementation for kernel tests.

Keeps the test surface free of filesystem I/O. Decision file stems live in
their own dict so :meth:`list_decisions` can return a sorted view without
introspecting the broader file table.
"""

from __future__ import annotations

from nauro_core.constants import DECISIONS_DIR

_DECISIONS_PREFIX = f"{DECISIONS_DIR}/"


class InMemoryStore:
    """Test double for :class:`~nauro_core.operations.store.Store`.

    Decisions are passed in keyed by file stem and exposed via
    :meth:`list_decisions` / :meth:`read_decision`. Other files live in a
    separate dict so write/read/delete round-trip without polluting the
    decision view.

    Writes whose path is under ``decisions/*.md`` are mirrored into the
    decision view so kernels that write a decision and then re-read it
    via :meth:`read_decision` round-trip the same way they do against
    ``FilesystemStore``.
    """

    def __init__(
        self,
        decisions: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        self._decisions: dict[str, str] = dict(decisions or {})
        self._files: dict[str, str] = dict(files or {})

    def read_file(self, path: str) -> str | None:
        stem = _decision_stem(path)
        if stem is not None and stem in self._decisions:
            return self._decisions[stem]
        return self._files.get(path)

    def write_file(self, path: str, content: str) -> None:
        stem = _decision_stem(path)
        if stem is not None:
            self._decisions[stem] = content
            return
        self._files[path] = content

    def delete_file(self, path: str) -> None:
        stem = _decision_stem(path)
        if stem is not None:
            self._decisions.pop(stem, None)
            return
        self._files.pop(path, None)

    def list_decisions(self) -> list[str]:
        return sorted(self._decisions)

    def read_decision(self, file_stem: str) -> str | None:
        return self._decisions.get(file_stem)

    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        # Dispatch through self.read_decision per stem rather than reading
        # self._decisions directly: subclasses that instrument read_decision
        # (e.g. the scan-counting double) must see one call per stem.
        return {stem: self.read_decision(stem) for stem in stems}


def _decision_stem(path: str) -> str | None:
    """Return the decision file stem when ``path`` targets ``decisions/*.md``."""
    if not path.startswith(_DECISIONS_PREFIX):
        return None
    tail = path[len(_DECISIONS_PREFIX) :]
    if "/" in tail or not tail.endswith(".md"):
        return None
    return tail[: -len(".md")]
