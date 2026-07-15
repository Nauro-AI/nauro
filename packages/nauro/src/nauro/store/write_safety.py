"""Symlink refusal for repo-scoped writes.

A cloned repository is untrusted content: a symlink pre-planted at a path
Nauro mutates (a write, a read-before-mutate, an unlink, a directory prune)
redirects the operation outside the repo. The rule is that repo-scoped
mutations never traverse a symlink, covering directory components (``.nauro``,
``.cursor``, ``.claude``, ``.codex`` as symlinks) and the final file. On
detection the writer refuses and warns, naming the path; it never writes
through the link and never replaces the link. The repo root itself is trusted
(the user's own choice); everything below it is checkout content. The
guarantee covers pre-planted symlinks, not TOCTOU races.
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
