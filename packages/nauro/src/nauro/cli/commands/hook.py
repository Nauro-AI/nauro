"""nauro hook — Manage the post-commit git hook for automatic extraction.

The hook runs `nauro extract` as a background post-commit side effect.
It must never block the developer's workflow and must never crash.
"""

from __future__ import annotations

import stat
from pathlib import Path

import typer

from nauro.constants import HOOK_END_MARKER, HOOK_START_MARKER

hook_app = typer.Typer(help="Manage the post-commit git hook.")

# Markers to identify the nauro section in a hook file
HOOK_START = HOOK_START_MARKER
HOOK_END = HOOK_END_MARKER

HOOK_SCRIPT = f"""{HOOK_START}
# Run nauro extraction in the background — must never block the commit
nauro extract > /dev/null 2>&1 &
{HOOK_END}"""


def _find_git_dir() -> Path:
    """Find .git/hooks in the current repo, walking up if needed."""
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        git_dir = parent / ".git"
        if git_dir.is_dir():
            return git_dir / "hooks"
    raise typer.BadParameter("Not inside a git repository.")


@hook_app.command()
def install() -> None:
    """Install the nauro post-commit hook in the current repo's .git/hooks/.

    Does not overwrite existing hooks — appends the nauro section.
    """
    hooks_dir = _find_git_dir()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    if hook_path.exists():
        content = hook_path.read_text()
        if HOOK_START in content:
            typer.echo(f"Nauro post-commit hook is already installed at {hook_path}")
            return
        # Append to existing hook
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + HOOK_SCRIPT + "\n"
        hook_path.write_text(content)
    else:
        hook_path.write_text("#!/bin/sh\n\n" + HOOK_SCRIPT + "\n")

    # Make executable
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    typer.echo(f"Installed nauro post-commit hook at {hook_path}")


@hook_app.command()
def uninstall() -> None:
    """Remove the nauro post-commit hook from the current repo."""
    hooks_dir = _find_git_dir()
    hook_path = hooks_dir / "post-commit"

    if not hook_path.exists():
        typer.echo("No post-commit hook found.")
        return

    content = hook_path.read_text()
    if HOOK_START not in content:
        typer.echo("No nauro hook section found in post-commit.")
        return

    # Remove the nauro section (including surrounding blank lines)
    lines = content.split("\n")
    result = []
    in_nauro = False
    for line in lines:
        if line.strip() == HOOK_START:
            in_nauro = True
            continue
        if line.strip() == HOOK_END:
            in_nauro = False
            continue
        if not in_nauro:
            result.append(line)

    new_content = "\n".join(result).strip()

    # If only the shebang remains, remove the file entirely
    if new_content in ("#!/bin/sh", "#!/bin/bash", ""):
        hook_path.unlink()
        typer.echo("Removed nauro post-commit hook (file deleted — no other hooks remained).")
    else:
        hook_path.write_text(new_content + "\n")
        typer.echo(f"Removed nauro section from {hook_path}")
