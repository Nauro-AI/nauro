"""What an S3 ETag can and cannot say about a local file.

An ETag equals the object's content MD5 only for a single-part upload that is
not KMS-encrypted. A multipart ETag (``<md5>-<parts>``) and an SSE-KMS or SSE-C
ETag are opaque, and a comparison against either means nothing. Both the cloud
restore and the sync pull have to draw that line, so it is drawn once here
rather than inside whichever of the two grew it first.
"""

from __future__ import annotations

import hashlib
from enum import Enum, auto
from pathlib import Path

_MD5_HEX_DIGITS = 32


def content_md5(raw_etag: str) -> str | None:
    """Return the ETag as a content MD5, or None when it cannot be one.

    An opaque ETag yields None so the caller skips the comparison rather than
    failing on it. Every other check a caller has - a published size, a
    published sha256 - still applies.
    """
    value = raw_etag.strip('"').lower()
    if len(value) != _MD5_HEX_DIGITS or any(ch not in "0123456789abcdef" for ch in value):
        return None
    return value


class ContentMatch(Enum):
    """What comparing a local file against an ETag established.

    ``unknown`` is not a near miss. It says the ETag carried no content digest,
    so nothing was compared and nothing is known. Folding it into ``differs``
    would let an opaque ETag pose as evidence that a file is out of date, which
    is the shape of guess this type exists to refuse.
    """

    matches = auto()
    differs = auto()
    unknown = auto()


def compare_local_file(path: Path, raw_etag: str) -> ContentMatch:
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
        return ContentMatch.unknown
    digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    return ContentMatch.matches if digest == expected else ContentMatch.differs
