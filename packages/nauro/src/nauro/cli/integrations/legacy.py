"""Legacy CLAUDE.md block cleanup for the setup surface."""

from __future__ import annotations

from pathlib import Path

from nauro.cli.integrations.outcomes import LegacyKind, LegacyOutcome
from nauro.constants import CLAUDE_MD, NAURO_BLOCK_END, NAURO_BLOCK_START
from nauro.store.reader import read_text_lenient
from nauro.store.write_safety import find_symlink

# Legacy markers — kept for removal of old CLAUDE.md blocks during --remove.
CLAUDE_MD_START = NAURO_BLOCK_START
CLAUDE_MD_END = NAURO_BLOCK_END


def _remove_claude_md(repo_path: Path) -> LegacyOutcome | None:
    """Remove a legacy Nauro block from CLAUDE.md if present.

    Returns an outcome if a block was removed or the write was refused, or
    None if there is no CLAUDE.md or no legacy block to remove.
    """
    refusal = find_symlink(repo_path, CLAUDE_MD)
    if refusal is not None:
        return LegacyOutcome(LegacyKind.REFUSED_SYMLINK, repo_path, refusal=refusal)
    claude_md = repo_path / CLAUDE_MD
    if not claude_md.exists():
        return None

    content = read_text_lenient(claude_md)
    if CLAUDE_MD_START not in content:
        return None

    before = content[: content.index(CLAUDE_MD_START)]
    after = content[content.index(CLAUDE_MD_END) + len(CLAUDE_MD_END) :]
    remaining = (before + after).strip()

    if not remaining:
        claude_md.unlink()
        return LegacyOutcome(LegacyKind.REMOVED_DELETED_FILE, repo_path)
    else:
        claude_md.write_text(remaining + "\n", encoding="utf-8")
        return LegacyOutcome(LegacyKind.REMOVED_BLOCK, repo_path)
