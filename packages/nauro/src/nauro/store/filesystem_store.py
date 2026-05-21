"""Filesystem-backed ``Store`` implementation for the local CLI + stdio MCP.

Adapts the existing ``~/.nauro/projects/<name>/`` layout to the kernel's
:class:`~nauro_core.operations.Store` protocol. Writes hold a FileLock to
match the rest of the local writer module; reads are unlocked.
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock
from nauro_core.constants import DECISIONS_DIR


class FilesystemStore:
    """Concrete ``Store`` rooted at a single project's on-disk directory.

    Paths passed to :meth:`read_file` / :meth:`write_file` / :meth:`delete_file`
    are interpreted relative to ``store_path``. Decision file enumeration goes
    through :meth:`list_decisions`; a stem returned there can be read via
    :meth:`read_decision` without re-deriving the canonical decisions
    sub-directory.
    """

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path

    def read_file(self, path: str) -> str | None:
        target = self._store_path / path
        if not target.exists():
            return None
        return target.read_text()

    # Sequential decision-file creation is owned by writer.py's existing FileLock
    # contract; this Store's write_file targets non-decision content paths.
    def write_file(self, path: str, content: str) -> None:
        target = self._store_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = target.with_name(target.name + ".lock")
        with FileLock(str(lock)):
            target.write_text(content)

    def delete_file(self, path: str) -> None:
        target = self._store_path / path
        if not target.exists():
            return
        target.unlink()

    def list_decisions(self) -> list[str]:
        decisions_dir = self._store_path / DECISIONS_DIR
        if not decisions_dir.exists():
            return []
        return sorted(f.stem for f in decisions_dir.glob("*.md"))

    def read_decision(self, file_stem: str) -> str | None:
        target = self._store_path / DECISIONS_DIR / f"{file_stem}.md"
        if not target.exists():
            return None
        return target.read_text()
