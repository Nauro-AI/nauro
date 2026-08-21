"""Stateless markdown → structured data parsers for non-decision files.

Decision parsing lives in ``nauro_core.decision_model.parse_decision``.
This module covers the smaller helpers — filename number extraction,
state/stack/questions parsing, and snippet extraction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from nauro_core.constants import DECISIONS_DIR, PROJECT_MD_SCAFFOLD_BODY, STACK_EMPTY_MARKER

# Tokens that end in a period without ending the sentence. A terminator
# closing one of these is treated as part of the abbreviation, not a sentence
# boundary, so "Should we e.g. cache?" is not clipped to "Should we e.g.".
# Lowercased and stripped of the trailing period for the comparison.
_SENTENCE_ABBREVIATIONS: frozenset[str] = frozenset({"e.g", "i.e", "vs", "etc", "cf"})

_SENTENCE_TERMINATORS = ".!?"


def first_sentence_end(text: str) -> int:
    """Index just past the first sentence boundary: a ``.``, ``!`` or ``?`` followed
    by whitespace or end of string. A period closing a known abbreviation or a
    single-letter initial is skipped; returns ``len(text)`` when no boundary exists.
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
    The token ending at the period (the run of non-space characters before it) is
    matched against the known-abbreviation set or a single lone letter.
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
    """First sentence of ``text``, terminator dropped, trimmed to ``length`` chars.
    ``...`` is appended when the first sentence ran longer than ``length``.
    """
    end = first_sentence_end(text)
    first_sentence = text[:end].rstrip(".!?")
    snippet = first_sentence[:length].strip()
    if len(first_sentence) > length:
        snippet += "..."
    return snippet


def _cap_to_first_unit(body: str) -> str:
    """Cap a stripped body to its first line or first sentence, whichever ends sooner.
    A multi-sentence single line truncates to its first sentence; a multi-line
    body to its first line.
    """
    text = body.strip()
    line_end = text.find("\n")
    if line_end == -1:
        line_end = len(text)
    cut = min(line_end, first_sentence_end(text))
    return text[:cut].rstrip()


_ASCII_DIGITS = "0123456789"


def is_ascii_decimal(token: str) -> bool:
    """True when ``token`` is a non-empty run of ASCII decimal digits.
    Not ``str.isdigit``: a non-ASCII digit either makes ``int`` raise or breaks the
    round-trip between a number and the ASCII text this store writes for it.
    """
    return bool(token) and all(char in _ASCII_DIGITS for char in token)


def _continues_a_number(char: str) -> bool:
    """True when ``char`` could be one more digit of the number just read.
    ``str.isdecimal`` is true for exactly the digits ``int`` accepts, so a decimal
    digit in any script continues the number; a superscript or circled digit does not.
    """
    return char.isdecimal()


def extract_decision_number(identifier: str) -> int | None:
    """Extract the decision number from ``"042-title[.md]"``, ``"decision-042"``,
    ``"D42"``, or a bare ``"42"``; None when no shape matches. Digits are ASCII and
    judged whole: a run continued by a non-ASCII decimal digit reads as no number.
    """
    s = identifier.removesuffix(".md")
    low = s.lower()
    if low.startswith("decision-"):
        s = s[len("decision-") :]
    elif low.startswith("d") and len(s) > 1 and is_ascii_decimal(s[1]):
        s = s[1:]
    leading = ""
    for ch in s:
        if is_ascii_decimal(ch):
            leading += ch
            continue
        if _continues_a_number(ch):
            return None
        break
    return int(leading) if leading else None


def sort_stems_by_number(stems: Iterable[str]) -> list[str]:
    """Order decision file stems by number ascending, then by stem.

    Stems carrying no number sort last, lexicographically among themselves.
    """
    return sorted(stems, key=_stem_order_key)


def _stem_order_key(stem: str) -> tuple[int, int, str]:
    """Sort key for :func:`sort_stems_by_number`: numbered stems first, by number."""
    num = extract_decision_number(stem)
    if num is None:
        return (1, 0, stem)
    return (0, num, stem)


# Body reference prefixes, lowercased. ``extract_decision_number`` accepts the
# same forms for a single identifier; this scanner finds every occurrence in a
# free-text body. The ``d`` form is matched case-insensitively to agree with
# that function, and the alphanumeric left-boundary guard keeps it from firing
# inside a longer token.
_REFERENCE_PREFIXES: tuple[str, ...] = ("decision-", "d")


def scan_decision_references(text: str, max_number: int) -> set[int]:
    """Return every decision number referenced in ``text`` as ``D70`` / ``decision-70``,
    case-insensitive where lowercasing preserves length (always for ASCII). A reference
    needs a non-alphanumeric left boundary, a whole ASCII digit run, and ``1..max_number``.
    """
    low = text.lower()
    found: set[int] = set()
    for prefix in _REFERENCE_PREFIXES:
        _scan_prefix(text, low, prefix, found, max_number)
    return found


def _scan_prefix(text: str, low: str, prefix: str, found: set[int], max_number: int) -> None:
    """Accumulate every ``<prefix><digits>`` occurrence in ``text`` into ``found``.
    ``low`` is ``text`` lowercased once for case-insensitive prefix matches; its offsets
    index ``text``, exact only where lowercasing preserves length (always for ASCII).
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
        # Right boundary, same rule as ``extract_decision_number``: a run that
        # stops at a digit from another script is one number written in two
        # scripts and references nothing, while a run that stops at a letter or
        # a space is an ordinary reference that ends there.
        mixed_script = i < n and _continues_a_number(text[i])
        if i > digit_start and not mixed_script:
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
    CRLF is normalized first; one H1 is removed when present and the remainder is
    returned stripped. Content without a leading H1 passes through stripped.
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
    """Whether ``content`` is an unedited ``nauro init`` project.md scaffold: a leading
    H1 is stripped and the remainder compared to ``PROJECT_MD_SCAFFOLD_BODY``
    exactly, so any edit to the body means the content is no longer scaffold-form.
    """
    return strip_leading_h1(content) == PROJECT_MD_SCAFFOLD_BODY.strip()


def extract_current_state(state_content: str) -> str:
    """Extract only the ``## Current`` section from state.md content.

    Legacy fallback for pre-upgrade files; new stores read state_current.md directly.
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
    """Whether ``line`` is a top-level ``- `` bullet: the stripped line opens with
    ``- `` and the raw line has no leading two-space indent (a nested child bullet).
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
