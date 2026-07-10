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
        target.write_text(existing.rstrip() + header + stripped + "\n", encoding="utf-8")
    else:
        target.write_text(header.lstrip() + stripped + "\n", encoding="utf-8")


def _parse_and_import_decisions(content: str, store_path: Path) -> int:
    """Parse decision blocks from decisionLog.md and create decision files.

    Expects ## Decision: <title> blocks. Each block's body becomes the rationale.

    Returns:
        Number of decisions imported.
    """
    # Split on ## Decision: headers. The title must begin with a non-whitespace
    # char (\S): this stops an empty heading ("## Decision:" with no title, with
    # or without trailing spaces) from either swallowing the newline to promote
    # the next body line to the title or capturing a lone space as a 1-char
    # title. Such a malformed heading is simply not treated as a decision
    # boundary.
    pattern = r"^## Decision:[ \t]*(\S.*)$"
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


def _import_adrs(
    adr_dir: Path,
    store_path: Path,
    *,
    strict_alternatives: bool = False,
) -> dict[str, Any]:
    """Import Architecture Decision Records from a directory into the store.

    Scans for markdown files matching ADR naming patterns (NNN-title.md or
    NNNN-title.md). Extracts title, rationale, rejected alternatives, and
    confidence from each file.

    Args:
        adr_dir: Path to directory containing ADR markdown files.
        store_path: Path to the target project store.
        strict_alternatives: When True, rejected alternatives come from
            ``_extract_adr_alternatives_strict`` — only ``### <title>``
            subsections under an explicit ``## Alternatives Considered`` /
            ``## Options Considered`` section, each carrying its verbatim body
            as the reason. No ``## Consequences`` scraping and no placeholder
            reason: an ADR that names no alternatives imports with no rejected
            list. Defaults to False, which selects the legacy
            ``_extract_adr_rejected`` path (unchanged in shape). Section
            extraction is heading-level-aware per ``_extract_section``, so h3
            ``### Rejected``/``### Consequences`` sections now reach the legacy
            path where they were previously dropped.

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
        confidence = _extract_adr_confidence(content)

        if strict_alternatives:
            structured_rejected = _extract_adr_alternatives_strict(content)
        else:
            structured_rejected = _legacy_structured_rejected(content)

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


def _legacy_structured_rejected(content: str) -> list[dict] | None:
    """Legacy default rejected-alternative shape for ``nauro import --adr``.

    ADR source format has no structured reason-per-alternative; attach a
    placeholder so the v2 validator (which requires a reason on every rejected
    alternative of an active decision) accepts the import. The placeholder makes
    the data gap explicit rather than fabricating prose.
    """
    rejected = _extract_adr_rejected(content)
    if not rejected:
        return None
    return [
        {
            "alternative": alt,
            "reason": "Rejected reason not available in source ADR.",
        }
        for alt in rejected
    ]


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


# A subsection title under "## Alternatives Considered" that opens with one of
# these deferred/conditional phrasings names an option the source explicitly
# left open, not a rejected one. A "may become valid later" alternative is not a
# named rejected-alternative span, so the strict extractor drops it rather than
# recording it as rejected with no honest rejection rationale.
# Forward-looking, held-open dispositions only. Phrase-anchored on purpose: a
# bare "revisit"/"reconsider" would also match past-tense rejection narration
# ("we revisited X and rejected it"), dropping a genuine rejection as held-open.
_DEFERRED_OPTION_MARKERS: tuple[str, ...] = (
    "may become",
    "might become",
    "may later",
    "maybe later",
    "could become",
    "to be decided",
    "tbd",
    "deferred",
    "defer until",
    "revisit later",
    "revisit after",
    "revisit when",
    "revisit once",
    "reconsider later",
    "reconsider once",
    "postpone",
)


def _extract_adr_alternatives_strict(content: str) -> list[dict] | None:
    """Extract NAMED rejected alternatives from an explicit alternatives section.

    Strict, alternatives-aware companion to ``_extract_adr_rejected`` — opt-in
    and never on the default ``nauro import --adr`` path. It reads only a
    ``## Alternatives Considered`` or ``## Options Considered`` section, splits
    it into ``### <title>`` subsections, and returns one entry per subsection
    whose body verbatim is the rejection reason::

        {"alternative": <title>, "reason": <verbatim body>, "offset": <int>}

    ``offset`` is the character index of the ``### `` heading line in
    ``content``, so a caller can derive a file:line citation for the span.

    Deliberately narrow, by design:

    - No ``## Consequences`` scraping — that fallback infers rejection from
      prose, which fabricates a "why" the source never stated.
    - When the source names no alternatives section, returns ``None`` (the
      caller omits ``rejected`` entirely — no placeholder reason).
    - A subsection whose body opens with a deferred/conditional marker (e.g.
      "This may become valid later") is an option held open, not a named
      rejection, and is skipped.

    Returns ``None`` when there is no alternatives section or it yields no
    honest named rejection; otherwise a non-empty list.
    """
    section = _find_alternatives_section(content)
    if section is None:
        return None
    section_text, section_start = section

    entries: list[dict] = []
    for title, body, rel_offset in _iter_subsections(section_text):
        if _is_deferred_option(body):
            continue
        if not body:
            continue
        entries.append(
            {
                "alternative": title,
                "reason": body,
                "offset": section_start + rel_offset,
            }
        )
    return entries or None


def _find_alternatives_section(content: str) -> tuple[str, int] | None:
    """Return ``(section_body, body_start_offset)`` for the alternatives section.

    Matches ``## Alternatives Considered`` or ``## Options Considered`` (case
    insensitive). ``body_start_offset`` is the character index in ``content``
    where the section body begins (just after the heading line). Returns
    ``None`` when neither heading is present.
    """
    pattern = re.compile(
        r"^##\s+(?:Alternatives|Options)\s+Considered\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(content)
    if not m:
        return None
    start = m.end()
    next_heading = re.search(r"^##\s+", content[start:], re.MULTILINE)
    body = content[start : start + next_heading.start()] if next_heading else content[start:]
    return body, start


def _iter_subsections(section_text: str):
    """Yield ``(title, verbatim_body, offset)`` per ``### <title>`` subsection.

    ``offset`` is the index of the ``###`` heading line within ``section_text``;
    ``verbatim_body`` is the stripped text between this heading and the next
    ``###`` (or end of section). The body is returned exactly as written — no
    rewording, no summarization — so the reason is a quoted span, never composed.
    """
    heading = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading.finditer(section_text))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        body = section_text[body_start:body_end].strip()
        yield title, body, m.start()


def _is_deferred_option(body: str) -> bool:
    """True when a subsection body opens with a deferred/conditional marker.

    Guards against recording an option the source held open ("may become valid
    later") as a rejected alternative. Only the opening of the body is checked —
    a marker buried mid-paragraph does not flip an otherwise-rejected option.
    """
    lowered = body.lstrip().lower()
    opening = lowered[:200]
    return any(marker in opening for marker in _DEFERRED_OPTION_MARKERS)


def _extract_adr_confidence(content: str) -> str:
    """Determine confidence from ## Status section."""
    status_text = _extract_section(content, "Status")
    if status_text:
        status_lower = status_text.lower().strip()
        if re.search(r"\b(accepted|approved)\b", status_lower):
            return "high"
    return "medium"


def _extract_section(content: str, heading: str) -> str | None:
    """Extract the body text of an h2 or h3 heading section, stripped.

    An h2 heading is preferred over h3: the heading is matched at level 2 first
    (``## <heading>``) and level 3 (``### <heading>``) is tried only when no h2
    of that name exists anywhere in the content, so an h2 section extracts
    identically to the h2-only rule even when a same-named ``###`` sits earlier
    in the content. The body runs up to the next heading at the matched level or
    shallower: for an h2 match the boundary is the next h2, so nested ``###``
    subsections stay in the body; for an h3 match the boundary is the next h2 or
    h3. h1 is never a boundary and never a section heading (the level floor is
    2); h4 and deeper never match as a heading and stay inside the body. Setext
    headings are not supported. Returns None if the section is not found or is
    empty.
    """
    for level in (2, 3):
        hashes = "#" * level
        pattern = rf"^{hashes}\s+{re.escape(heading)}\s*$"
        m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
        if not m:
            continue

        start = m.end()
        # Boundary is the next heading at the matched level or shallower.
        boundary = re.compile(rf"^#{{2,{level}}}\s+", re.MULTILINE)
        next_heading = boundary.search(content[start:])
        body = content[start : start + next_heading.start()] if next_heading else content[start:]

        body = body.strip()
        return body if body else None

    return None


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
    """Import context from an external source into the project store.

    Each import captures a snapshot; AGENTS.md in the associated repos is
    not refreshed here — run 'nauro sync' afterwards.
    """
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
