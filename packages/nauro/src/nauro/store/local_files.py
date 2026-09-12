"""Typed reads of local files status and setup inspect: absent, readable, or unreadable.

A missing file, or a missing or non-directory ancestor, is absent. Any other failure to
read is UnreadableFileError carrying the path and the reason, never a quiet default.
"""

from pathlib import Path


class UnreadableFileError(Exception):
    """A file that exists but could not be read, with the path and the reason."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def read_text_or_absent(path: Path, *, errors: str = "strict") -> str | None:
    """Read ``path`` as UTF-8: None when absent, UnreadableFileError when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors=errors)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise UnreadableFileError(path, str(exc)) from exc


def is_regular_file(path: Path) -> bool:
    """Whether ``path`` is a regular file; a path that cannot be inspected raises."""
    try:
        return path.is_file()
    except OSError as exc:
        raise UnreadableFileError(path, str(exc)) from exc
