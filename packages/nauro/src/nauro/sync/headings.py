"""Decision-heading format primitives shared by the sync corpus and classifier.

A decision file's identity is its filename number; its H1 repeats that number.
The writer emits one canonical shape, ``# NNN — Title`` with the number
zero-padded to three digits, while the parser is deliberately laxer and accepts
an unpadded or over-padded number and arbitrary leading whitespace. Renumbering
has to respect the narrower shape: a file whose heading is not in the canonical
form is one this layer must not rewrite, because a partial or missed rewrite
would leave a decision whose heading and filename disagree.

Both questions - "what number does this heading claim?" and "may I rewrite
it?" - are answered here, from one matcher, so the classification gate and the
write can never drift apart.
"""

from __future__ import annotations

_HEADING_SEPARATORS = ("—", "-")


def heading_number(text: str) -> int | None:
    """Return the number in the first decision H1, if the text has one.

    Deliberately as lax as the decision parser: any digit run counts, so a
    heading this module refuses to rewrite is still *read* correctly and a
    filename/heading disagreement is detected rather than missed.
    """
    for line in text.splitlines():
        number = heading_line_number(line)
        if number is not None:
            return number
    return None


def heading_line_number(line: str) -> int | None:
    """Return the decision number one line claims as an H1, if it is one."""
    if not line.startswith("#"):
        return None
    after_hash = line[1:]
    rest = after_hash.lstrip(" ")
    if len(rest) == len(after_hash):
        # No space after the hash: "#Not a heading", or a deeper "## " level.
        return None
    head, _, tail = rest.partition(" ")
    if not head.isdigit() or not tail.startswith(_HEADING_SEPARATORS):
        return None
    return int(head)


def _rewritable_index(lines: list[str], num: int) -> int | None:
    """Index of the first heading line the rewriter may address, if any."""
    prefix = f"# {num:03d}"
    for i, line in enumerate(lines):
        if line.startswith(f"{prefix} —") or line.startswith(f"{prefix} -"):
            return i
    return None


def has_rewritable_heading(text: str, num: int) -> bool:
    """True when :func:`rewrite_decision_heading` would change this text's H1."""
    return _rewritable_index(text.splitlines(keepends=True), num) is not None


def rewrite_decision_heading(text: str, old_num: int, new_num: int) -> str:
    """Rewrite the first canonical ``# NNN —`` / ``# NNN -`` H1 to ``new_num``.

    Only the first line in canonical form is rewritten; the separator and the
    rest of the line are preserved byte-for-byte. Text with no such line is
    returned unchanged, so callers verify the result rather than assuming the
    rewrite landed.
    """
    lines = text.splitlines(keepends=True)
    index = _rewritable_index(lines, old_num)
    if index is None:
        return text
    old_prefix = f"# {old_num:03d}"
    lines[index] = f"# {new_num:03d}" + lines[index][len(old_prefix) :]
    return "".join(lines)


def strip_number_prefix(filename: str) -> str:
    """Drop a leading ``NNN-`` decision-number prefix from a filename.

    The leading digit run plus the single hyphen that immediately follows it
    are removed. A digit run with no trailing hyphen is left intact.
    """
    idx = 0
    while idx < len(filename) and filename[idx].isdigit():
        idx += 1
    if idx > 0 and idx < len(filename) and filename[idx] == "-":
        return filename[idx + 1 :]
    return filename


__all__ = [
    "has_rewritable_heading",
    "heading_line_number",
    "heading_number",
    "rewrite_decision_heading",
    "strip_number_prefix",
]
