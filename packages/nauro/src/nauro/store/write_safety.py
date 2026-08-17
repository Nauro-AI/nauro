"""Symlink refusal for repo-scoped and user-global writes.

Repo scope: a cloned repository is untrusted content, so a symlink pre-planted
at a path Nauro mutates redirects the operation outside the repo. Repo-scoped
mutations never traverse a symlink, covering directory components (``.nauro``,
``.cursor``, ``.claude``, ``.codex``) and the final file (:func:`find_symlink`).
The repo root itself is trusted; everything below it is checkout content.

User scope: a file under the user's home is the user's own content, but replacing
a symlinked file would sever it from the real file a dotfile manager owns, so a
user-global mutation refuses only when the final component is itself a symlink
(:func:`find_file_symlink`) and allows symlinked parents.

On detection the writer refuses and warns, naming the path. The guarantee covers
pre-planted symlinks, not TOCTOU races.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymlinkRefusal:
    """A refused repo-scoped mutation.

    ``target`` is the path the caller wanted to mutate; ``link`` is the
    offending symlink component (may equal ``target``).
    """

    target: Path
    link: Path

    @property
    def message(self) -> str:
        if self.link == self.target:
            return (
                f"refused to modify {self.target}: it is a symlink; "
                "Nauro does not write through symlinks in a repo checkout"
            )
        return (
            f"refused to modify {self.target}: {self.link} is a symlink; "
            "Nauro does not write through symlinks in a repo checkout"
        )


@dataclass(frozen=True)
class UserSymlinkRefusal:
    """A refused user-global mutation: ``target`` itself is a symlink."""

    target: Path

    @property
    def message(self) -> str:
        return (
            f"refused to modify {self.target}: it is a symlink; "
            "Nauro does not replace symlinked user files "
            "(a dotfile manager may own the real file)"
        )


def find_file_symlink(path: Path) -> UserSymlinkRefusal | None:
    """Return a refusal when ``path`` itself is a symlink, else ``None``.

    One lstat check on the final component, so a symlinked parent keeps working.
    """
    if path.is_symlink():
        return UserSymlinkRefusal(target=path)
    return None


def find_symlink(repo_root: Path, relative: str) -> SymlinkRefusal | None:
    """Return a refusal for the first symlink component of ``repo_root / relative``.

    Walks every component below ``repo_root`` with lstat, never resolving the suffix.
    """
    target = repo_root / relative
    current = repo_root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return SymlinkRefusal(target=target, link=current)
    return None
