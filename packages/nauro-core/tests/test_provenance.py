from __future__ import annotations

import pytest

from nauro_core.provenance import (
    InvalidCommitSha,
    InvalidUtcTimestamp,
    validate_commit_sha,
    validate_utc_timestamp,
)


@pytest.mark.parametrize("value", ["a" * 40, "b" * 64])
def test_validate_commit_sha_accepts_lowercase_full_hashes(value: str) -> None:
    assert validate_commit_sha(value, field="proposed_base_commit") == value


@pytest.mark.parametrize("value", ["A" * 40, "a" * 39, "g" * 40, "a" * 41])
def test_validate_commit_sha_rejects_noncanonical_hashes(value: str) -> None:
    with pytest.raises(InvalidCommitSha):
        validate_commit_sha(value, field="proposed_base_commit")


def test_validate_utc_timestamp_accepts_pinned_microsecond_form() -> None:
    value = "2026-08-14T09:10:11.123456Z"
    assert validate_utc_timestamp(value, field="approved_at") == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-14T09:10:11Z",
        "2026-08-14T09:10:11.123456+00:00",
        "2026-02-30T09:10:11.123456Z",
        "2026-08-14t09:10:11.123456Z",
    ],
)
def test_validate_utc_timestamp_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(InvalidUtcTimestamp):
        validate_utc_timestamp(value, field="approved_at")
