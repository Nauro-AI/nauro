"""What an S3 ETag can and cannot say about a local file.

An ETag equals the object's content MD5 only for a single-part upload that is
not encrypted with SSE-KMS or SSE-C. Only one of those exclusions is visible in
the ETag itself: a multipart ETag wears its part count (``<md5>-<parts>``) and
is rejected here. An encrypted object's ETag is 32 hexadecimal characters like
any other, so nothing can tell it apart, and it is compared like any other. It
simply never matches, which costs a fetch and is never a false match.

Both the cloud restore and the sync pull have to draw that line, so it is drawn
once here rather than inside whichever of the two grew it first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

_MD5_HEX_DIGITS = 32


def content_md5(raw_etag: str) -> str | None:
    """Return the ETag as a content MD5, or None when it cannot be one.

    An ETag this can see is not a digest yields None, so the caller skips the
    comparison rather than failing on it. Every other check a caller has - a
    published size, a published sha256 - still applies.
    """
    value = raw_etag.strip('"').lower()
    if len(value) != _MD5_HEX_DIGITS or any(ch not in "0123456789abcdef" for ch in value):
        return None
    return value


class ContentMatch(Enum):
    """What comparing a local file against an ETag established.

    ``unknown`` is not a near miss. It says the ETag carried no content digest,
    so nothing was compared and nothing is known. Folding it into ``differs``
    would let an ETag that is not a digest pose as evidence that a file is out
    of date, which is the shape of guess this type exists to refuse.
    """

    matches = auto()
    differs = auto()
    unknown = auto()


@dataclass(frozen=True)
class LocalComparison:
    """What one read of a local file established about it.

    ``sha256`` is filled on ``matches`` alone, and it is taken from the same
    read as the comparison, so the two provably describe one set of bytes. A
    caller that hashed the file again to record it would leave a window between
    the two reads: an edit landing there is recorded as already synced, and
    then goes nowhere, because the push skips a file whose recorded digest
    matches and the pull ignores one whose ETag has not moved.
    """

    match: ContentMatch
    sha256: str = ""


def compare_local_file(path: Path, raw_etag: str) -> LocalComparison:
    """Say whether ``path`` already holds the bytes the ETag describes.

    A file this cannot read raises rather than answering. Every caller hashes
    the same file a step later and would raise on the same fault, so catching
    it here would buy nothing and cost the distinction: a reported IO error
    would become a silent verdict about content nobody read.

    Raises:
        OSError: the file could not be read.
    """
    expected = content_md5(raw_etag)
    if expected is None:
        return LocalComparison(ContentMatch.unknown)
    content = path.read_bytes()
    if hashlib.md5(content, usedforsecurity=False).hexdigest() != expected:
        return LocalComparison(ContentMatch.differs)
    return LocalComparison(ContentMatch.matches, hashlib.sha256(content).hexdigest())
