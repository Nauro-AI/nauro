"""Quarantine records for decision-number collisions the pull cannot resolve.

A quarantined collision leaves the local decision file untouched and never
records sync state for the remote file, so the collision is re-detected and
re-reported on every pull until a human resolves it. The only durable artifact
is the remote file's bytes, dropped into ``.conflict-backup/`` under a name
keyed by the remote version rather than the wall clock: re-pulling the same
remote decision reproduces the same name, so repeated SessionStart pulls leave
exactly one backup instead of one per session.

The same naming makes the backup directory the state surface both status
commands read. A quarantine counts as resolved once the remote path it names
carries a sync-state entry, which happens only when a later pull installed that
file for real.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from pathlib import Path

from nauro.sync.collisions import is_canonical_decision_path
from nauro.sync.merge import CONFLICT_BACKUP_DIR, write_backup
from nauro.sync.state import SyncState, load_state

_BACKUP_PREFIX = "number-collision-"

# Characters a backup filename may carry unescaped. Everything else is
# percent-encoded, so the name is a single flat component on every filesystem.
_SAFE_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "._-")

# The two digests plus the prefix and separators account for 51 characters, so
# this keeps the whole component under 200 - well inside the 255-byte limit
# common to every filesystem this runs on.
_MAX_ENCODED_PATH = 145

# The remote ETag is a header value: it carries quotes, may be absent, and is
# not a safe filename on every platform. A fixed-width digest of it keys the
# backup instead: same one-name-per-remote-version property, always a legal
# filename.
_KEY_LENGTH = 16


@dataclass(frozen=True)
class QuarantinedCollision:
    """One quarantined remote decision, recovered from its backup filename.

    ``remote_path`` is for display and may be a prefix of the path the server
    held, since a very long name is capped in the filename. ``path_digest`` is
    over the whole path and is what identity is decided on.
    """

    remote_path: str
    path_digest: str
    backup_path: Path

    @property
    def complete_path(self) -> bool:
        """False when the displayed path was capped in the backup filename."""
        return path_key(self.remote_path) == self.path_digest

    @property
    def label(self) -> str:
        return self.remote_path if self.complete_path else f"{self.remote_path}..."


def _digest(material: bytes) -> str:
    """The fixed-width, filename-safe key this module identifies things by."""
    return hashlib.sha256(material).hexdigest()[:_KEY_LENGTH]


def quarantine_key(remote_etag: str, remote_content: bytes) -> str:
    """Return the backup key identifying one remote version of a decision."""
    return _digest(remote_etag.encode("utf-8") if remote_etag else remote_content)


def path_key(remote_path: str) -> str:
    """Return the key identifying one exact remote path.

    The encoded path already appears in the filename, but on a case-folding
    filesystem two paths that differ only in case name one physical file - so
    two distinct quarantines would share a backup, and installing the canonical
    one would appear to resolve the other. This digest is over the path's exact
    bytes and renders as lowercase hex, which no filesystem folds together, so
    distinct paths always get distinct files.
    """
    return _digest(remote_path.encode("utf-8"))


def _encode_path(remote_path: str) -> str:
    """Flatten a store path into one filename component without losing it.

    Percent-encoding over an allowlist rather than escaping the separators we
    happened to think of: a quarantined path is server-supplied and can carry
    anything, and everything outside the safe set is a filename hazard
    somewhere - a backslash is a separator on Windows and would send the write
    into a directory that does not exist, a colon opens an alternate data
    stream, a trailing dot or space is silently stripped. Encoding by byte also
    keeps a non-ASCII path from depending on the filesystem's normalisation.
    """
    encoded = "".join(
        chr(byte) if chr(byte) in _SAFE_NAME_CHARS else f"%{byte:02X}"
        for byte in remote_path.encode("utf-8")
    )
    if encoded.endswith("."):
        # Windows strips a trailing dot, so the name written would not be the
        # name computed and the backup would multiply on every pull.
        encoded = encoded[:-1] + "%2E"
    return encoded


def _decode_path(encoded: str) -> str:
    """Inverse of :func:`_encode_path`, tolerant of a truncated tail."""
    out = bytearray()
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == "%" and index + 2 <= len(encoded):
            hex_digits = encoded[index + 1 : index + 3]
            if len(hex_digits) == 2:
                try:
                    out.append(int(hex_digits, 16))
                except ValueError:
                    out.extend(char.encode("utf-8"))
                    index += 1
                    continue
                index += 3
                continue
        out.extend(char.encode("utf-8"))
        index += 1
    return out.decode("utf-8", errors="replace")


def _truncate_encoded(encoded: str) -> str:
    """Cap the encoded path so the whole filename stays well inside any limit.

    Identity lives in the two digests, so a capped tail costs display
    completeness and nothing else. The cut never lands inside a percent escape,
    which would decode to a different byte than the one encoded.
    """
    if len(encoded) <= _MAX_ENCODED_PATH:
        return encoded
    cut = _MAX_ENCODED_PATH
    for start in range(cut - 2, cut):
        if 0 <= start < len(encoded) and encoded[start] == "%":
            cut = start
            break
    return encoded[:cut]


def backup_name(remote_path: str, key: str) -> str:
    encoded = _truncate_encoded(_encode_path(remote_path))
    return f"{_BACKUP_PREFIX}{key}-{path_key(remote_path)}-{encoded}"


def save_quarantine_backup(
    store_path: Path, remote_path: str, remote_content: bytes, remote_etag: str
) -> Path:
    """Drop the quarantined remote decision into the backup directory.

    An existing backup for the same remote version is left alone, so a re-pull is idempotent.
    """
    name = backup_name(remote_path, quarantine_key(remote_etag, remote_content))
    existing = store_path / CONFLICT_BACKUP_DIR / name
    if existing.exists():
        return existing
    return write_backup(store_path, name, remote_content)


def _parse_backup_name(name: str) -> tuple[str, str] | None:
    """Recover the path digest and displayable path from a backup filename.

    Only this module writes the prefix, so the prefix and key shape are enough
    to tell a quarantine copy from a last-write-wins backup; the path itself is
    whatever was encoded, including spellings this store would never write.
    """
    if not name.startswith(_BACKUP_PREFIX):
        return None
    rest = name[len(_BACKUP_PREFIX) :]
    keys: list[str] = []
    for _position in range(2):
        if len(rest) <= _KEY_LENGTH or rest[_KEY_LENGTH] != "-":
            return None
        keys.append(rest[:_KEY_LENGTH])
        rest = rest[_KEY_LENGTH + 1 :]
    decoded = _decode_path(rest)
    if not decoded:
        return None
    return keys[1], decoded


def list_quarantine_backups(store_path: Path) -> list[QuarantinedCollision]:
    """Every quarantine backup on disk, resolved or not, sorted by remote path."""
    backup_dir = store_path / CONFLICT_BACKUP_DIR
    if not backup_dir.is_dir():
        return []
    found = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        parsed = _parse_backup_name(entry.name)
        if parsed is not None:
            digest, remote_path = parsed
            found.append(
                QuarantinedCollision(remote_path=remote_path, path_digest=digest, backup_path=entry)
            )
    return sorted(found, key=lambda item: (item.remote_path, item.backup_path.name))


def unresolved_quarantines(
    store_path: Path, state: SyncState | None = None
) -> list[QuarantinedCollision]:
    """Quarantined collisions whose remote decision is still not installed.

    Matched on the path digest; only a canonically spelled path can resolve one.
    """
    backups = list_quarantine_backups(store_path)
    if not backups:
        return []
    tracked = (state or load_state(store_path)).files
    installed = {path_key(rel) for rel in tracked if is_canonical_decision_path(rel)}
    return [item for item in backups if item.path_digest not in installed]


__all__ = [
    "QuarantinedCollision",
    "backup_name",
    "list_quarantine_backups",
    "quarantine_key",
    "save_quarantine_backup",
    "unresolved_quarantines",
]
