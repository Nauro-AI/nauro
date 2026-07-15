"""Symlink refusal for repo-scoped and user-global writes.

Repo scope: a cloned repository is untrusted content, so a symlink pre-planted
at a path Nauro mutates (a write, a read-before-mutate, an unlink, a
directory prune) redirects the operation outside the repo. Repo-scoped
mutations never traverse a symlink, covering directory components (``.nauro``,
``.cursor``, ``.claude``, ``.codex`` as symlinks) and the final file
(:func:`find_symlink`). The repo root itself is trusted (the user's own
choice); everything below it is checkout content.

User scope: files under the user's home directory (``~/.codex/config.toml``,
``~/.claude.json``, skill and agent files) are the user's own content, but
replacing a symlinked file would sever it from the real file a dotfile
manager owns. User-global mutations refuse only when the final path component
is itself a symlink (:func:`find_file_symlink`); symlinked parent directories
are expected and allowed; dotfile managers routinely symlink whole config
directories.

On detection the writer refuses and warns, naming the path; it never writes
through the link and never replaces the link. The guarantee covers
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
    """Return a refusal when ``path`` itself is a symlink, else None.

    Exactly one lstat-based check on the final component; parent directories
    are deliberately not walked, so a symlinked ``~/.codex`` or ``~/.claude``
    keeps working. ``Path.is_symlink`` never follows the link, so a dangling
    symlink is still refused. A missing path is safe.
    """
    if path.is_symlink():
        return UserSymlinkRefusal(target=path)
    return None


def find_symlink(repo_root: Path, relative: str) -> SymlinkRefusal | None:
    """Return a refusal for the first symlink component of ``repo_root / relative``.

    Walks each component from the first one below ``repo_root`` down to and
    including the final path, using the lstat-based ``Path.is_symlink()``,
    which never follows links. A missing component is safe: there is nothing
    to traverse. The untrusted suffix is never resolved.
    """
    target = repo_root / relative
    current = repo_root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return SymlinkRefusal(target=target, link=current)
    return None
