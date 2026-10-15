"""Directory links as each platform makes them, and a link-aware tree snapshot."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LINK_KINDS = ["symlink", "junction"]


def link_directory(link: Path, target: Path, kind: str) -> None:
    """Point ``link`` at ``target`` as a symlink or as a Windows junction."""
    if kind == "junction":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
        return
    link.symlink_to(target, target_is_directory=True)


def require_link_kind(kind: str) -> None:
    if (kind == "junction") != (sys.platform == "win32"):
        pytest.skip(f"{kind} is not this platform's directory link")


def tree(root):
    """Every entry under ``root`` keyed by relative path: link target, directory, or bytes."""
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink()))
            if path.is_symlink()
            else ("dir", None)
            if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
    }
