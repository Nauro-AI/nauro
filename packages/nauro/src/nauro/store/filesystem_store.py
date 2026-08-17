"""Filesystem-backed ``Store`` implementation for the local CLI + stdio MCP.

Adapts the existing ``~/.nauro/projects/<name>/`` layout to the kernel's
:class:`~nauro_core.operations.Store` protocol. Writes hold a per-target
FileLock and land atomically through :mod:`nauro.store._atomic`; reads are
unlocked.
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock
from nauro_core.constants import DECISIONS_DIR

from nauro.store._atomic import atomic_write_text
from nauro.store.reader import read_text_lenient


def canonical_store_relative_path(store_path: Path, path: str) -> str | None:
    """Return ``path`` in canonical POSIX store-relative form, ``None`` if it escapes.

    Read-side companion to ``_resolve_within``: alias spellings cannot dodge a rule.
    """
    try:
        target = (store_path / path).resolve()
        return target.relative_to(store_path.resolve()).as_posix()
    except (ValueError, OSError):
        return None


class FilesystemStore:
    """Concrete ``Store`` rooted at a single project's on-disk directory.

    Paths passed to :meth:`read_file`, :meth:`write_file` and :meth:`delete_file`
    are interpreted relative to ``store_path``. A decision stem from
    :meth:`list_decisions` reads back through :meth:`read_decision`.
    """

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path

    def _resolve_within(self, path: str) -> Path:
        """Return ``path`` resolved against the store root, refusing any escape.

        Raises ``ValueError`` for a ``..`` or absolute path landing outside the store.
        """
        target = (self._store_path / path).resolve()
        target.relative_to(self._store_path.resolve())  # raises ValueError on escape
        return target

    def read_file(self, path: str) -> str | None:
        try:
            target = self._resolve_within(path)
        except ValueError:
            return None
        if not target.exists() or not target.is_file():
            return None
        return read_text_lenient(target)

    # Per-write FileLock only — no cross-file lock to serialize decision
    # numbering across concurrent writers. Collisions (two writers minting
    # the same num because they raced between list/write) are caught and
    # repaired on the next sync-pull.
    def write_file(self, path: str, content: str) -> None:
        """Replace ``path`` atomically: readers see old bytes or new bytes, never a mix."""
        # Fail loud on an out-of-store path: silently dropping or redirecting a
        # write would corrupt the store, so a traversal path is an error.
        target = self._resolve_within(path)
        # Ahead of the lock, because the lock file is a sibling of the target.
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = target.with_name(target.name + ".lock")
        with FileLock(str(lock)):
            atomic_write_text(target, content)

    def delete_file(self, path: str) -> None:
        try:
            target = self._resolve_within(path)
        except ValueError:
            return
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
        return read_text_lenient(target)

    # Serial loop, no thread pool: local disk reads are fast and a pool would
    # contend with the per-write FileLock. Byte-identical to scanning the
    # stems one at a time. Cloud transports override this to fan out.
    def read_decisions(self, stems: list[str]) -> dict[str, str | None]:
        return {stem: self.read_decision(stem) for stem in stems}
