"""Validation for provenance values shared across local and hosted writers."""

from __future__ import annotations

from datetime import datetime


class InvalidCommitSha(ValueError):
    """A repository commit is not a canonical full SHA-1 or SHA-256 value."""


class InvalidUtcTimestamp(ValueError):
    """A timestamp is not the pinned RFC3339 UTC representation."""


def validate_commit_sha(value: object, *, field: str) -> str:
    """Return a lowercase 40- or 64-character hexadecimal commit SHA."""
    if not isinstance(value, str):
        raise InvalidCommitSha(f"{field} must be a string.")
    if len(value) not in (40, 64) or any(char not in "0123456789abcdef" for char in value):
        raise InvalidCommitSha(
            f"{field} must be a 40- or 64-character lowercase hexadecimal commit SHA."
        )
    return value


def validate_utc_timestamp(value: object, *, field: str) -> str:
    """Return a timestamp in exact ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` form."""
    if not isinstance(value, str) or len(value) != 27:
        raise InvalidUtcTimestamp(f"{field} must use YYYY-MM-DDTHH:MM:SS.ffffffZ.")
    if value[10] != "T" or value[19] != "." or value[-1] != "Z":
        raise InvalidUtcTimestamp(f"{field} must use YYYY-MM-DDTHH:MM:SS.ffffffZ.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise InvalidUtcTimestamp(
            f"{field} must be a valid UTC timestamp in YYYY-MM-DDTHH:MM:SS.ffffffZ form."
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise InvalidUtcTimestamp(f"{field} must use canonical YYYY-MM-DDTHH:MM:SS.ffffffZ form.")
    return value
