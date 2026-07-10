"""Stateless markdown → structured data parsers for non-decision files.

Decision parsing lives in ``nauro_core.decision_model.parse_decision``.
This module covers the smaller helpers — filename number extraction,
state/stack/questions parsing, and snippet extraction.
"""

from __future__ import annotations

import re

from nauro_core.constants import DECISIONS_DIR, PROJECT_MD_SCAFFOLD_BODY, STACK_EMPTY_MARKER

# Tokens that end in a period without ending the sentence. A terminator
# closing one of these is treated as part of the abbreviation, not a sentence
# boundary, so "Should we e.g. cache?" is not clipped to "Should we e.g.".
# Lowercased and stripped of the trailing period for the comparison.
_SENTENCE_ABBREVIATIONS: frozenset[str] = frozenset({"e.g", "i.e", "vs", "etc", "cf"})

_SENTENCE_TERMINATORS = ".!?"


def first_sentence_end(text: str) -> int:
    """Index just past the end of the first sentence in ``text``.

    A sentence ends at a ``.``, ``!`` or ``?`` that is followed by whitespace
    or the end of the string. A terminator immediately followed by a non-space
    (a decimal point, a mid-word ellipsis) is not a boundary, and a terminating
    period that closes a known abbreviation (``e.g.``, ``vs.``, ``etc.``) or a
    single letter is skipped so the sentence runs past it. Returns ``len(text)``
    when no boundary is found. Plain string ops; no regex.

    Shared by the graph payload builder (first-sentence body cap) and BM25
    snippet generation so the two surfaces split sentences identically.
    """
    n = len(text)
    for i, ch in enumerate(text):
        if ch not in _SENTENCE_TERMINATORS:
            continue
        if i + 1 < n and not text[i + 1].isspace():
            continue
        if ch == "." and _ends_with_abbreviation(text, i):
            continue
        return i + 1
    return n


def _ends_with_abbreviation(text: str, period_idx: int) -> bool:
    """Whether the period at ``period_idx`` closes an abbreviation or initial.

    Reads the token ending at the period (the run of non-space characters
    immediately before it, with any embedded periods kept so ``e.g`` is one
    token) and reports a match against the known-abbreviation set or a
    single-letter initial.
    """
    start = period_idx
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:period_idx].lower()
    if not token:
        return False
    if token in _SENTENCE_ABBREVIATIONS:
        return True
    # A single trailing letter ("A." in an initial) with no other letters.
    return len(token) == 1 and token.isalpha()


def _first_sentence_snippet(text: str, length: int = 100) -> str:
    """First sentence of ``text``, trimmed to ``length`` with a trailing ellipsis.

    Splits at the first sentence boundary via the co-located ``first_sentence_end``,
    drops the trailing terminator, trims to ``length`` characters, and appends
    ``...`` when the first sentence runs longer than ``length``. This is the BM25
    search snippet fallback used when no query word matches the rationale, so its
    output shape is user-visible on the search surface.
    """
    end = first_sentence_end(text)
    first_sentence = text[:end].rstrip(".!?")
    snippet = first_sentence[:length].strip()
    if len(first_sentence) > length:
        snippet += "..."
    return snippet


def _cap_to_first_unit(body: str) -> str:
    """Cap a body to its first sentence or first line, whichever ends sooner.

    The first line ends at the first newline; the first sentence ends per the
    co-located ``first_sentence_end`` grammar (terminator plus boundary,
    abbreviations skipped). The shorter boundary wins, so a multi-sentence
    single line truncates to the first sentence and a multi-line body to its
    first line. Feeds the graph payload's open-question display bodies.
    """
    text = body.strip()
    line_end = text.find("\n")
    if line_end == -1:
        line_end = len(text)
    cut = min(line_end, first_sentence_end(text))
    return text[:cut].rstrip()


def extract_decision_number(identifier: str) -> int | None:
    """Extract the decision number from a decision identifier.

    Accepts:
    - file stem: ``"042-some-title"`` (or ``"042-some-title.md"``)
    - synthetic id: ``"decision-042"``
    - prefixed: ``"D042"`` or ``"D42"``
    - bare integer: ``"42"``

    Returns None if the identifier doesn't match a known shape.
    """
    s = identifier.removesuffix(".md")
    low = s.lower()
    if low.startswith("decision-"):
        s = s[len("decision-") :]
    elif low.startswith("d") and len(s) > 1 and s[1].isdigit():
        s = s[1:]
    leading = ""
    for ch in s:
        if ch.isdigit():
            leading += ch
        else:
            break
    return int(leading) if leading else None


_ASCII_DIGITS = "0123456789"

# Body reference prefixes, lowercased. ``extract_decision_number`` accepts the
# same forms for a single identifier; this scanner finds every occurrence in a
# free-text body. The ``d`` form is matched case-insensitively to agree with
# that function, and the alphanumeric left-boundary guard keeps it from firing
# inside a longer token.
_REFERENCE_PREFIXES: tuple[str, ...] = ("decision-", "d")


def scan_decision_references(text: str, max_number: int) -> set[int]:
    """Return every in-range decision number referenced in ``text``.

    Recognized forms, case-insensitive: ``D70`` / ``d70``, zero-padded
    ``D070``, and ``decision-70`` / ``Decision-70``. A reference must be
    preceded by a non-alphanumeric character (or the start of the text) so a
    digit or letter run does not fabricate a match (a UUID's ``...d4`` or an
    identifier's ``...D70`` is rejected). The digit run is read to its boundary
    so a short reference never matches inside a longer number (``D118`` parses
    as 118, never 1 then 18), and only ASCII digits are consumed so a trailing
    Unicode digit cannot reach ``int`` and raise. Numbers outside
    ``1..max_number`` are dropped. Plain string ops; no regex.

    This is the single home for the body-reference grammar. The graph payload
    builder is the current consumer; ``extract_decision_number`` stays the
    single-identifier analogue.
    """
    low = text.lower()
    found: set[int] = set()
    for prefix in _REFERENCE_PREFIXES:
        _scan_prefix(text, low, prefix, found, max_number)
    return found


def _scan_prefix(text: str, low: str, prefix: str, found: set[int], max_number: int) -> None:
    """Accumulate every ``<prefix><digits>`` occurrence into ``found``.

    ``low`` is ``text`` lowercased once so the prefix match is case-insensitive
    without rescanning; offsets line up because lowercasing is length-preserving
    for the ASCII letters in the prefixes. The original ``text`` supplies the
    digit run.
    """
    plen = len(prefix)
    n = len(text)
    start = low.find(prefix)
    while start != -1:
        # Left boundary: the char before the prefix must not be alphanumeric,
        # else this is the tail of a longer token (an identifier ending in "d",
        # a UUID digit run, a longer number). The digit run after the prefix is
        # then read whole, which is also the prefix-collision guard.
        if start > 0 and text[start - 1].isalnum():
            start = low.find(prefix, start + 1)
            continue
        i = start + plen
        digit_start = i
        while i < n and text[i] in _ASCII_DIGITS:
            i += 1
        if i > digit_start:
            value = int(text[digit_start:i])
            if 1 <= value <= max_number:
                found.add(value)
        # i is always at least start + plen >= start + 1, so resuming the scan
        # at i never re-examines the just-handled prefix.
        start = low.find(prefix, i)


# Decision id / filename formatting. These are the write-side counterparts to
# extract_decision_number: they render a decision number or file stem into the
# canonical id, label, filename-prefix, filename, and store-path forms used
# across the operations kernel. Module-private (not exported in a public API)
# so callers converge on one spelling of each form.


def _canonical_decision_id(num: int) -> str:
    """Render ``num`` as the canonical ``decision-NNN`` retrieval id."""
    return f"decision-{num:03d}"


def _decision_label(num: int) -> str:
    """Render ``num`` as the short ``DNNN`` display label."""
    return f"D{num:03d}"


def _decision_number_prefix(num: int) -> str:
    """Render ``num`` as the ``NNN-`` file-stem prefix."""
    return f"{num:03d}-"


def _decision_filename(stem: str) -> str:
    """Render a decision file stem as its ``<stem>.md`` filename."""
    return f"{stem}.md"


def _decision_path(stem: str) -> str:
    """Render a decision file stem as its store-relative ``decisions/<stem>.md`` path."""
    return f"{DECISIONS_DIR}/{stem}.md"


def _stem_from_decision_path(path: str) -> str | None:
    """Return the decision file stem when ``path`` targets ``decisions/*.md``."""
    prefix = f"{DECISIONS_DIR}/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix) :]
    if "/" in tail or not tail.endswith(".md"):
        return None
    return tail[: -len(".md")]


def strip_leading_h1(content: str) -> str:
    """Drop a leading ``# ...`` H1 line plus surrounding blank lines.

    Mirrors ``context._strip_leading_current_header`` but for any H1: leading
    blank lines are dropped, one H1 line is removed when present, and the
    remainder is returned stripped. Content without a leading H1 passes
    through (stripped) unchanged. Shared by ``is_scaffold_project_md`` and the
    L0 project-scope preamble, which renders project.md under its own section
    ordering and must not stutter the file's title heading.
    """
    # S3-synced bytes on the hosted path decode without the universal-newline
    # translation local Path.read_text gets; normalizing here fixes both the
    # scaffold-guard comparison and preamble rendering for CRLF files.
    content = content.replace("\r\n", "\n")
    lines = content.strip().split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def is_scaffold_project_md(content: str) -> bool:
    """Whether ``content`` is an unedited ``nauro init`` project.md scaffold.

    The scaffold interpolates only the ``# {project_name}`` heading, so the
    check strips a leading H1 and compares the remainder against
    ``PROJECT_MD_SCAFFOLD_BODY`` exactly (trailing whitespace stripped on both
    sides). Any edit to the body — even one character — means the user has
    started filling in real scope and the content is no longer scaffold-form.
    """
    return strip_leading_h1(content) == PROJECT_MD_SCAFFOLD_BODY.strip()


def extract_current_state(state_content: str) -> str:
    """Extract only the ``## Current`` section from state.md content.

    Legacy fallback only — used when reading pre-upgrade state.md files
    that contain both ``## Current`` and ``## History`` sections. New-format
    stores use state_current.md directly.
    """
    lines = state_content.split("\n")
    in_current = False
    current_lines: list[str] = []
    for line in lines:
        if line.strip().lower() == "## current":
            in_current = True
            continue
        if in_current and line.startswith("## "):
            break
        if in_current:
            current_lines.append(line)
    return "\n".join(current_lines).strip()


def _is_top_level_bullet(line: str) -> bool:
    """Whether ``line`` is a top-level ``- `` bullet, not an indented child.

    The stripped line opens with ``- `` and the raw line carries no leading
    indent, so a nested bullet under a parent item does not count. Shared by the
    stack extractors here and the snapshot stack diff so the three sites agree on
    what a top-level bullet is.
    """
    return line.strip().startswith("- ") and not line.startswith("  ")


def extract_stack_oneliner(stack_content: str) -> str:
    """Extract a one-line stack summary listing only technology names."""
    if not stack_content.strip() or stack_content.strip() == STACK_EMPTY_MARKER:
        return ""

    names: list[str] = []
    for line in stack_content.split("\n"):
        stripped = line.strip()
        if _is_top_level_bullet(line):
            m = re.match(r"-\s+\*\*(.+?)\*\*", stripped)
            if m:
                names.append(m.group(1))
    return ", ".join(names)


def extract_stack_summary(stack_content: str) -> str:
    """Extract one-line tech choices from stack.md (no full reasoning)."""
    if not stack_content.strip() or stack_content.strip() == STACK_EMPTY_MARKER:
        return ""

    lines = []
    for line in stack_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") or stripped.startswith("<!--"):
            continue
        if _is_top_level_bullet(line) or stripped.startswith("## "):
            lines.append(stripped)
    return "\n".join(lines)


def decisions_summary_lines(decisions: list, limit: int = 10) -> list[str]:
    """Build compact summary lines for decisions: ``D{num} — Title (date)``."""
    lines = []
    for d in decisions[:limit]:
        date_part = f" ({d.date})" if d.date else ""
        lines.append(f"- D{d.num} \u2014 {d.title}{date_part}")
    return lines


def extract_relevance_snippet(text: str, query_words: list[str], length: int = 100) -> str:
    """Extract ~length chars of context around the first query word match in text.

    Returns empty string if no match is found.
    """
    text_lower = text.lower()
    for word in query_words:
        pos = text_lower.find(word.lower())
        if pos != -1:
            half = length // 2
            start = max(0, pos - half)
            end = min(len(text), pos + len(word) + half)
            snippet = text[start:end].strip()
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(text) else ""
            return f"{prefix}{snippet}{suffix}"
    return ""
