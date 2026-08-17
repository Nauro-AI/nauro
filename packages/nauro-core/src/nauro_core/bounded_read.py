"""Bounded rendering of raw store file reads.

Pure, total helpers behind :mod:`nauro_core.renderers`: given a file's content
and its canonical store-relative path, produce the bounded form the rendered
agent channel emits. The CLI's default json output stays uncapped.

Content at or under its budget passes through byte-verbatim. Budgets bound
retained content only; elision markers are bounded-constant annotations naming
the omitted char count and the recovery path. Cuts land on the nearest newline
inside the retained window, with a hard cut at the budget when the window holds
no newline. An extraction-eligible discovery-pointer entry is retained or
omitted whole, never split. ``open-questions.md`` gets a structure-aware split;
content that defeats it falls back to the plain head-keep rule. Never raises.
"""

from __future__ import annotations

from dataclasses import dataclass

from nauro_core.constants import (
    BOUNDED_READ_RECOVERY,
    CONTEXT_FILE_CHAR_BUDGET,
    OPEN_PREAMBLE_HEAD_CHARS,
    OPEN_PREAMBLE_TAIL_CHARS,
    OPEN_QUESTIONS_MD,
    OPEN_QUESTIONS_MID_ELISION_MARKER,
    OPEN_QUESTIONS_POST_CUT_MARKER,
    OPEN_QUESTIONS_POST_ELIDED_MARKER,
    OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER,
    POINTER_BLOCK_HEADER,
    POINTER_EXTRACT_LIMIT,
    POINTER_OVERFLOW_TRAILER,
    RAW_FILE_CHAR_BUDGET,
    RAW_FILE_HEAD_CUT_MARKER,
    RAW_FILE_TAIL_CUT_MARKER,
    TAIL_KEEP_FILENAMES,
)
from nauro_core.questions import (
    EntryBlock,
    OpenQuestionsFile,
    QuestionEntry,
    truncate_entry_text,
)

_RESOLVED_HEADER_EXACT = "## Resolved"
_UNKNOWN_PATH_PLACEHOLDER = "<path>"
_CONTEXT_DIR_PREFIX = "context/"


def render_bounded_file(content: str, path: str | None) -> str:
    """Bounded render of one raw-file read, classified by canonical path. ``path`` is
    the canonical store-relative path, or ``None`` to force the generic head-keep
    rule; content at or under its budget returns byte-verbatim.
    """
    budget = _budget_for(path)
    if len(content) <= budget:
        return content
    if path == OPEN_QUESTIONS_MD:
        try:
            return _bounded_open_questions(content, path)
        except Exception:
            # Totality contract: a structural split failure must never
            # propagate into the renderer fallback (which would emit the
            # full unbounded envelope). Degrade to the plain head-keep rule.
            return _head_keep(content, budget, path)
    if path in TAIL_KEEP_FILENAMES:
        return _tail_keep(content, budget, path)
    return _head_keep(content, budget, path)


def _budget_for(path: str | None) -> int:
    if path is not None and path.startswith(_CONTEXT_DIR_PREFIX) and path.endswith(".md"):
        return CONTEXT_FILE_CHAR_BUDGET
    return RAW_FILE_CHAR_BUDGET


def _display_path(path: str | None) -> str:
    return path if path is not None else _UNKNOWN_PATH_PLACEHOLDER


def _recovery(path: str | None) -> str:
    return BOUNDED_READ_RECOVERY.format(path=_display_path(path))


def _head_keep(content: str, budget: int, path: str | None) -> str:
    window = content[:budget]
    newline = window.rfind("\n")
    end = newline if newline != -1 else budget
    marker = RAW_FILE_HEAD_CUT_MARKER.format(
        omitted=f"{len(content) - end:,}",
        path=_display_path(path),
        recovery=_recovery(path),
    )
    return content[:end] + "\n" + marker


def _tail_keep(content: str, budget: int, path: str | None) -> str:
    naive = len(content) - budget
    window = content[naive:]
    newline = window.find("\n")
    start = naive + newline + 1 if newline != -1 else naive
    marker = RAW_FILE_TAIL_CUT_MARKER.format(
        omitted=f"{start:,}",
        path=_display_path(path),
        recovery=_recovery(path),
    )
    return marker + "\n" + content[start:]


@dataclass(frozen=True)
class _Span:
    """Half-open character span ``[start, end)`` in the source text."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class _Window:
    """Retained sub-span ``[start, end)`` of one region."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, span: _Span) -> bool:
        return span.start >= self.start and span.end <= self.end


@dataclass(frozen=True)
class _LocatedPointer:
    """An extraction-eligible discovery-pointer entry and its source span.

    Eligible means: an open (no ``resolved_by`` annotation) discovery-pointer
    entry sitting in open content - entries physically under ``## Resolved``
    are retired history and never extracted.
    """

    span: _Span
    entry: QuestionEntry


@dataclass(frozen=True)
class _Layout:
    """Region map of open-questions.md content under the exact-header rule.

    ``preamble`` is the open content before the ``## Resolved`` header (the whole
    file when no exact header exists). ``resolved`` runs from that header to the
    next section header, exclusive. ``post`` is the rest; the spans tile the source.
    """

    preamble: _Span
    resolved: _Span | None
    post: _Span | None
    post_section_count: int
    pointers: tuple[_LocatedPointer, ...]


@dataclass(frozen=True)
class _ElidedSpan:
    """One elision point: its marker plus the pointers omitted with it."""

    marker: str
    pointers: tuple[_LocatedPointer, ...]


def _bounded_open_questions(content: str, path: str) -> str:
    layout = _locate_layout(content)
    if layout is None:
        return _head_keep(content, RAW_FILE_CHAR_BUDGET, path)
    recovery = _recovery(path)

    preamble = layout.preamble
    preamble_pointers = [p for p in layout.pointers if p.span.end <= preamble.end]
    post_pointers = (
        [p for p in layout.pointers if p.span.start >= layout.post.start]
        if layout.post is not None
        else []
    )

    pieces: list[str | _ElidedSpan] = []

    if preamble.length <= RAW_FILE_CHAR_BUDGET:
        rendered_preamble = preamble.length
        pieces.append(content[preamble.start : preamble.end])
    else:
        head = _head_window(content, preamble, OPEN_PREAMBLE_HEAD_CHARS, preamble_pointers)
        tail = _tail_window(content, preamble, OPEN_PREAMBLE_TAIL_CHARS, preamble_pointers)
        rendered_preamble = head.length + tail.length
        omitted = preamble.length - rendered_preamble
        mid_pointers = tuple(
            p for p in preamble_pointers if not (head.contains(p.span) or tail.contains(p.span))
        )
        if head.length:
            pieces.append(content[head.start : head.end])
        pieces.append(
            _ElidedSpan(
                marker=OPEN_QUESTIONS_MID_ELISION_MARKER.format(
                    omitted=f"{omitted:,}", recovery=recovery
                ),
                pointers=mid_pointers,
            )
        )
        if tail.length:
            pieces.append(content[tail.start : tail.end])

    if layout.resolved is not None:
        pieces.append(
            OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER.format(
                omitted=f"{layout.resolved.length:,}", recovery=recovery
            )
        )

    if layout.post is not None:
        post = layout.post
        remaining = RAW_FILE_CHAR_BUDGET - rendered_preamble
        if remaining <= 0:
            pieces.append(
                _ElidedSpan(
                    marker=OPEN_QUESTIONS_POST_ELIDED_MARKER.format(
                        count=layout.post_section_count,
                        omitted=f"{post.length:,}",
                        recovery=recovery,
                    ),
                    pointers=tuple(post_pointers),
                )
            )
        elif post.length <= remaining:
            pieces.append(content[post.start : post.end])
        else:
            head = _head_window(content, post, remaining, post_pointers)
            omitted = post.length - head.length
            cut_pointers = tuple(p for p in post_pointers if not head.contains(p.span))
            if head.length:
                pieces.append(content[head.start : head.end])
            pieces.append(
                _ElidedSpan(
                    marker=OPEN_QUESTIONS_POST_CUT_MARKER.format(
                        omitted=f"{omitted:,}", recovery=recovery
                    ),
                    pointers=cut_pointers,
                )
            )

    return _assemble(pieces, recovery)


def _locate_layout(content: str) -> _Layout | None:
    """Map the parsed block model onto character spans of the source.

    Returns ``None`` unless every block covers exactly ``len(render_lines())`` lines.
    """
    lines = content.split("\n")
    parsed = OpenQuestionsFile.parse(content)

    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1

    cursor = _header_line_count(lines)
    ranges: list[tuple[int, int]] = []
    for block in parsed.blocks:
        count = len(block.render_lines())
        ranges.append((cursor, cursor + count))
        cursor += count
    if cursor != len(lines):
        return None

    total = len(content)

    def entry_span(block_idx: int) -> _Span:
        first_line, end_line = ranges[block_idx]
        return _Span(offsets[first_line], offsets[end_line - 1] + len(lines[end_line - 1]))

    # Exact-header rule over source lines, not parsed HeaderBlocks: the
    # comparison is whitespace-stripped, and the parser only mints a
    # HeaderBlock for lines that begin directly with "## " — an indented
    # "  ## Resolved" satisfies the rule but never becomes a block. The
    # section still ends at the next section-header line, exclusive.
    resolved_line: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == _RESOLVED_HEADER_EXACT:
            resolved_line = idx
            break

    post_line: int | None = None
    if resolved_line is not None:
        for idx in range(resolved_line + 1, len(lines)):
            if lines[idx].lstrip().startswith("## "):
                post_line = idx
                break

    if resolved_line is None:
        preamble = _Span(0, total)
        resolved = None
        post = None
        section_count = 0
    else:
        resolved_start = offsets[resolved_line]
        preamble = _Span(0, resolved_start)
        if post_line is None:
            resolved = _Span(resolved_start, total)
            post = None
            section_count = 0
        else:
            post_start = offsets[post_line]
            resolved = _Span(resolved_start, post_start)
            post = _Span(post_start, total)
            section_count = sum(1 for line in lines[post_line:] if line.lstrip().startswith("## "))

    pointers: list[_LocatedPointer] = []
    for idx, block in enumerate(parsed.blocks):
        if not isinstance(block, EntryBlock):
            continue
        entry = block.entry
        if entry.resolved_by is not None or not entry.is_discovery_pointer:
            continue
        span = entry_span(idx)
        in_resolved_region = resolved is not None and resolved.start <= span.start < resolved.end
        if in_resolved_region:
            continue
        pointers.append(_LocatedPointer(span=span, entry=entry))

    return _Layout(preamble, resolved, post, section_count, tuple(pointers))


def _header_line_count(lines: list[str]) -> int:
    """Lines the parser consumes before the first block (blanks + ``# `` header)."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].startswith("# ") and not lines[i].startswith("## "):
        i += 1
    return i


def _head_window(
    content: str, region: _Span, budget: int, pointers: list[_LocatedPointer]
) -> _Window:
    """Retained head window of ``region``: newline-aligned cut, whole-entry safe."""
    if budget <= 0:
        return _Window(region.start, region.start)
    if region.length <= budget:
        return _Window(region.start, region.end)
    window_text = content[region.start : region.start + budget]
    newline = window_text.rfind("\n")
    cut = region.start + (newline if newline != -1 else budget)
    for pointer in pointers:
        if pointer.span.start < cut < pointer.span.end:
            cut = max(region.start, pointer.span.start - 1)
            break
    return _Window(region.start, cut)


def _tail_window(
    content: str, region: _Span, budget: int, pointers: list[_LocatedPointer]
) -> _Window:
    """Retained tail window ending at ``region``'s last character."""
    if budget <= 0:
        return _Window(region.end, region.end)
    if region.length <= budget:
        return _Window(region.start, region.end)
    naive = region.end - budget
    window_text = content[naive : region.end]
    newline = window_text.find("\n")
    start = naive + newline + 1 if newline != -1 else naive
    for pointer in pointers:
        if pointer.span.start < start < pointer.span.end:
            start = min(region.end, pointer.span.end + 1)
            break
    return _Window(start, region.end)


def _assemble(pieces: list[str | _ElidedSpan], recovery: str) -> str:
    """Join retained text, markers, and per-span pointer blocks in file order.

    :data:`POINTER_EXTRACT_LIMIT` is allocated across spans; one trailer reports overflow.
    """
    parts: list[str] = []
    slots = POINTER_EXTRACT_LIMIT
    overflow = 0
    for piece in pieces:
        if isinstance(piece, str):
            if piece:
                parts.append(piece)
            continue
        parts.append(piece.marker)
        extracted = piece.pointers[:slots] if slots > 0 else ()
        overflow += len(piece.pointers) - len(extracted)
        slots -= len(extracted)
        if extracted:
            block_lines = [POINTER_BLOCK_HEADER]
            block_lines.extend(truncate_entry_text("\n".join(p.entry.render())) for p in extracted)
            parts.append("\n".join(block_lines))
    if overflow:
        parts.append(POINTER_OVERFLOW_TRAILER.format(overflow=overflow, recovery=recovery))
    return "\n".join(parts)
