"""nauro import — Import context from Cline/Roo Memory Bank or ADR directories.

v1 supports two import sources:
  --memory-bank <path>  Migrate a Cline/Roo Code Memory Bank (.context/ directory)
  --adr <path>          Migrate Architecture Decision Records (NNN-title.md files)
"""

import re
from pathlib import Path
from typing import Any

import typer
from nauro_core.constants import STATE_CURRENT_FILENAME
from nauro_core.operations import update_state as _update_state_op
from nauro_core.operations.propose_decision import _write_decision_direct

from nauro.cli.utils import resolve_target_project
from nauro.constants import PROJECT_MD, STACK_MD
from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.snapshot import capture_snapshot
from nauro.store.store_lock import store_write_lock


def _read(path: Path) -> str:
    """Read an import source file, replacing any undecodable bytes.

    ADR and Memory-Bank sources are often legacy docs (cp1252/latin-1), or a
    directory carries a stray binary file. Strict UTF-8 would abort the whole
    migration with a traceback on the first bad byte; errors="replace" lets the
    import proceed and the user see what came through.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _import_append_decision(
    store_path: Path,
    title: str,
    rationale: str | None = None,
    rejected: list[dict] | None = None,
    confidence: str = "medium",
) -> None:
    """Adapter for the import paths: write a decision via the kernel."""
    # Hold the allocation lock across the number computation and the write so
    # concurrent local writers cannot mint the same decision number. The
    # post-loop capture_snapshot stays outside the lock.
    with decision_write_lock(store_path):
        _write_decision_direct(
            FilesystemStore(store_path),
            {
                "title": title,
                "rationale": rationale,
                "rejected": rejected,
                "confidence": confidence,
            },
        )


def _import_memory_bank(memory_bank: Path, store_path: Path) -> dict[str, int]:
    """Import a Cline/Roo Code Memory Bank (.context/ directory) into the store.

    Maps Memory Bank files to Nauro store files:
      projectBrief.md  → project.md           (appended under ## Imported from Memory Bank)
      techContext.md   → stack.md             (appended under ## Imported from Memory Bank)
      decisionLog.md   → decisions/NNN-title.md (one file per ## Decision block)
      activeContext.md + progress.md → state_current.md (single composed update_state call)

    activeContext.md and progress.md are composed into one delta and written via
    a single update_state() call. Calling update_state per progress item would
    archive all but the last to state_history.md, which build_l0 ignores
    (include_history=False), making the imported state invisible to L0.

    Args:
        memory_bank: Path to the .context/ directory.
        store_path: Path to the target project store.

    Returns:
        Dict with counts of imported items by type.
    """
    counts: dict[str, int] = {
        "files_merged": 0,
        "decisions": 0,
        "progress_items": 0,
        # Set when decisionLog.md had content but no block matched the expected
        # heading, so the caller can warn instead of reporting a silent success.
        "decisionlog_unparsed": 0,
    }

    # projectBrief.md → project.md
    brief_path = memory_bank / "projectBrief.md"
    if brief_path.exists():
        _append_to_store_file(
            store_path / PROJECT_MD,
            _read(brief_path),
        )
        counts["files_merged"] += 1

    # techContext.md → stack.md
    tech_path = memory_bank / "techContext.md"
    if tech_path.exists():
        _append_to_store_file(
            store_path / STACK_MD,
            _read(tech_path),
        )
        counts["files_merged"] += 1

    # decisionLog.md → decisions/NNN-title.md
    decision_log = memory_bank / "decisionLog.md"
    if decision_log.exists():
        log_text = _read(decision_log)
        counts["decisions"] = _parse_and_import_decisions(log_text, store_path)
        if counts["decisions"] == 0 and log_text.strip():
            counts["decisionlog_unparsed"] = 1

    # activeContext.md + progress.md → state_current.md (one composed update_state)
    active_body: str | None = None
    active_path = memory_bank / "activeContext.md"
    if active_path.exists():
        active_body = _strip_h1_prefix(_read(active_path))
        if active_body:
            counts["files_merged"] += 1

    progress_items: list[str] = []
    progress_path = memory_bank / "progress.md"
    if progress_path.exists():
        progress_items = _import_progress(_read(progress_path))
    counts["progress_items"] = len(progress_items)

    delta = _compose_state_delta(active_body, progress_items)
    if delta is not None:
        # update_state rewrites state_current.md and read-appends
        # state_history.md; hold one lock across the whole kernel call so a
        # concurrent local writer cannot drop an update on either file.
        with store_write_lock(store_path, STATE_CURRENT_FILENAME):
            _update_state_op(FilesystemStore(store_path), delta)

    return counts


def _strip_h1_prefix(content: str) -> str:
    """Strip a leading H1 header (and trailing blank lines), then strip whitespace.

    activeContext.md typically opens with `# Active Context`, which becomes
    redundant once composed into a state delta — prepare_state_update wraps
    the delta in `# Current State` already.
    """
    lines = content.split("\n")
    first = lines[0].strip() if lines else ""
    if first.startswith("# ") and first[2:].strip():
        i = 1
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        return "\n".join(lines[i:]).strip()
    return content.strip()


def _compose_state_delta(active_body: str | None, progress_items: list[str]) -> str | None:
    """Compose activeContext body + progress items into a single state delta.

    Returns None when both inputs are empty (caller skips update_state entirely).
    """
    has_active = bool(active_body)
    has_progress = bool(progress_items)
    progress_block = "## Recently completed\n" + "\n".join(f"- {item}" for item in progress_items)
    if has_active and has_progress:
        return f"{active_body}\n\n{progress_block}"
    if has_active:
        return active_body
    if has_progress:
        return progress_block
    return None


def _append_to_store_file(target: Path, content: str) -> None:
    """Append imported content to an existing store markdown file.

    Adds a ## Imported from Memory Bank header before the content.
    If the file doesn't exist, creates it with just the imported content.
    """
    header = "\n\n## Imported from Memory Bank\n\n"
    stripped = content.strip()
    if not stripped:
        return

    if target.exists():
        existing = _read(target)
        target.write_text(existing.rstrip() + header + stripped + "\n")
    else:
        target.write_text(header.lstrip() + stripped + "\n")


def _parse_and_import_decisions(content: str, store_path: Path) -> int:
    """Parse decision blocks from decisionLog.md and create decision files.

    Expects ## Decision: <title> blocks. Each block's body becomes the rationale.

    Returns:
        Number of decisions imported.
    """
    # Split on ## Decision: headers
    pattern = r"^## Decision:\s*(.+)$"
    blocks = re.split(pattern, content, flags=re.MULTILINE)

    # blocks[0] is preamble (before first ## Decision:), then alternating title/body
    count = 0
    for i in range(1, len(blocks), 2):
        title = blocks[i].strip()
        body = blocks[i + 1].strip() if i + 1 < len(blocks) else ""
        rationale = body if body else None
        _import_append_decision(store_path, title, rationale=rationale)
        count += 1

    return count


def _import_adrs(adr_dir: Path, store_path: Path) -> dict[str, Any]:
    """Import Architecture Decision Records from a directory into the store.

    Scans for markdown files matching ADR naming patterns (NNN-title.md or
    NNNN-title.md). Extracts title, rationale, rejected alternatives, and
    confidence from each file.

    Args:
        adr_dir: Path to directory containing ADR markdown files.
        store_path: Path to the target project store.

    Returns:
        Dict with counts: imported, skipped.
    """
    counts: dict[str, Any] = {"imported": 0, "skipped": 0}
    skipped_reasons: list[str] = []

    # Find ADR files matching NNN- or NNNN- patterns
    adr_pattern = re.compile(r"^(\d{3,4})-.+\.md$")
    adr_files: list[tuple[int, Path]] = []

    for md_file in adr_dir.glob("*.md"):
        m = adr_pattern.match(md_file.name)
        if m:
            adr_files.append((int(m.group(1)), md_file))
        # Non-matching .md files are silently ignored (not ADRs)

    # Sort by ADR number to preserve original ordering
    adr_files.sort(key=lambda x: x[0])

    for _num, adr_path in adr_files:
        content = _read(adr_path)
        title = _extract_adr_title(content)
        if not title:
            counts["skipped"] += 1
            skipped_reasons.append(f"{adr_path.name}: no title heading found")
            continue

        rationale = _extract_adr_rationale(content)
        rejected = _extract_adr_rejected(content)
        confidence = _extract_adr_confidence(content)

        # ADR source format has no structured reason-per-alternative; attach a
        # placeholder so the v2 validator (which requires a reason on every
        # rejected alternative of an active decision) accepts the import. The
        # placeholder makes the data gap explicit rather than fabricating prose.
        structured_rejected: list[dict] | None = None
        if rejected:
            structured_rejected = [
                {
                    "alternative": alt,
                    "reason": "Rejected reason not available in source ADR.",
                }
                for alt in rejected
            ]

        _import_append_decision(
            store_path,
            title=title,
            rationale=rationale,
            rejected=structured_rejected,
            confidence=confidence,
        )
        counts["imported"] += 1

    counts["_skipped_reasons"] = skipped_reasons
    return counts


def _extract_adr_title(content: str) -> str | None:
    """Extract title from the first # heading, stripping any leading number prefix."""
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if not m:
        return None
    title = m.group(1).strip()
    # Strip leading number prefix like "1. " or "0001 - " or "1: "
    title = re.sub(r"^\d+[\.\:\-\s]+\s*", "", title).strip()
    return title if title else None


def _extract_adr_rationale(content: str) -> str | None:
    """Extract rationale from ## Context or ## Decision sections."""
    rationale_parts = []
    for section_name in ["Context", "Decision"]:
        text = _extract_section(content, section_name)
        if text:
            rationale_parts.append(text)

    return "\n\n".join(rationale_parts) if rationale_parts else None


def _extract_adr_rejected(content: str) -> list[str] | None:
    """Extract rejected alternatives from ## Rejected or ## Consequences sections."""
    # Try explicit ## Rejected section first
    for section_name in ["Rejected", "Rejected Alternatives", "Rejected Options"]:
        text = _extract_section(content, section_name)
        if text:
            return _extract_list_items(text)

    # Fall back to ## Consequences — look for rejected alternatives mentioned there
    consequences = _extract_section(content, "Consequences")
    if consequences:
        items = _extract_list_items(consequences)
        # Only return items that look like they mention rejected alternatives
        pat = r"reject|not\s+chos|instead\s+of|ruled\s+out|alternative"
        rejected = [item for item in items if re.search(pat, item, re.IGNORECASE)]
        if rejected:
            return rejected

    return None


def _extract_adr_confidence(content: str) -> str:
    """Determine confidence from ## Status section."""
    status_text = _extract_section(content, "Status")
    if status_text:
        status_lower = status_text.lower().strip()
        if re.search(r"\b(accepted|approved)\b", status_lower):
            return "high"
    return "medium"


def _extract_section(content: str, heading: str) -> str | None:
    """Extract the body text of a ## heading section.

    Returns the text between the given ## heading and the next ## heading
    (or end of file), stripped. Returns None if the section is not found
    or is empty.
    """
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None

    start = m.end()
    # Find next ## heading or end of content
    next_heading = re.search(r"^##\s+", content[start:], re.MULTILINE)
    body = content[start : start + next_heading.start()] if next_heading else content[start:]

    body = body.strip()
    return body if body else None


def _extract_list_items(text: str) -> list[str]:
    """Extract markdown list items from text."""
    items = []
    for line in text.split("\n"):
        line = line.strip()
        m = re.match(r"^[-*]\s+(.+)$", line)
        if m:
            items.append(m.group(1).strip())
    return items


def _import_progress(content: str) -> list[str]:
    """Extract recently completed items from progress.md as a list.

    Pure parser — composition into a state delta and the single update_state
    call live in _import_memory_bank.
    """
    items: list[str] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip()
            if item:
                items.append(item)
    return items


_Opt_memory_bank = typer.Option(
    None,
    "--memory-bank",
    help=(
        "Path to a Cline/Roo Code Memory Bank (.context/ directory). "
        "decisionLog.md entries must use '## Decision: <title>' headings to import."
    ),
)
_Opt_adr = typer.Option(
    None,
    "--adr",
    help="Path to an Architecture Decision Records directory (files named '<NNN>-title.md').",
)


def import_cmd(
    memory_bank: Path | None = _Opt_memory_bank,
    adr: Path | None = _Opt_adr,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name. Overrides cwd resolution.",
    ),
) -> None:
    """Import context from an external source into the project store."""
    if memory_bank is None and adr is None:
        typer.echo(
            "Error: specify --memory-bank <path> or --adr <path>. See nauro import --help",
            err=True,
        )
        raise typer.Exit(code=1)

    project_name, store_path = resolve_target_project(project)

    if memory_bank is not None:
        mb = Path(memory_bank)
        if not mb.is_dir():
            typer.echo(f"Error: '{memory_bank}' is not a directory.", err=True)
            raise typer.Exit(code=1)
        if not (mb / "projectBrief.md").exists():
            typer.echo(
                f"Error: '{memory_bank}' does not contain projectBrief.md. "
                "Not a valid Memory Bank directory.",
                err=True,
            )
            raise typer.Exit(code=1)

        counts = _import_memory_bank(mb, store_path)
        capture_snapshot(store_path, trigger="import: memory-bank")

        typer.echo(f"Imported Memory Bank into {project_name}:")
        typer.echo(f"  Store: {store_path}")
        typer.echo(f"  {counts['files_merged']} file(s) merged")
        typer.echo(f"  {counts['decisions']} decision(s) imported")
        typer.echo(f"  {counts['progress_items']} progress item(s) imported")
        if counts.get("decisionlog_unparsed"):
            typer.echo(
                "  Warning: decisionLog.md had content but no entries matched the "
                "expected '## Decision: <title>' heading — 0 decisions imported. "
                "Re-check the heading format (see 'nauro import --help').",
                err=True,
            )
        typer.echo("  Next: run 'nauro sync' to update AGENTS.md in associated repos")

    if adr is not None:
        adr_path = Path(adr)
        if not adr_path.is_dir():
            typer.echo(f"Error: '{adr}' is not a directory.", err=True)
            raise typer.Exit(code=1)

        adr_counts = _import_adrs(adr_path, store_path)
        capture_snapshot(store_path, trigger="import: adr")

        typer.echo(f"Imported ADRs into {project_name}:")
        typer.echo(f"  Store: {store_path}")
        typer.echo(f"  {adr_counts['imported']} ADR(s) imported")
        typer.echo(f"  {adr_counts['skipped']} ADR(s) skipped")
        for reason in adr_counts.get("_skipped_reasons", []):
            typer.echo(f"    - {reason}")
        if adr_counts["imported"] == 0 and adr_counts["skipped"] == 0:
            typer.echo(
                "  Warning: no ADRs imported. Files must be named '<NNN>-title.md' "
                "(e.g. 0001-use-postgres.md); other .md files are ignored.",
                err=True,
            )
        typer.echo("  Next: run 'nauro sync' to update AGENTS.md in associated repos")
