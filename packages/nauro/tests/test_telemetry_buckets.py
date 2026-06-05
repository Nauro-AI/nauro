"""Boundary tests for telemetry duration/byte bucketing.

The bucket labels are part of the locked PRIVACY.md taxonomy, so the exact
strings are asserted here — a label rename is a wire-format change, not a
cosmetic one.
"""

from __future__ import annotations

import pytest

from nauro.telemetry._buckets import bucket, byte_bucket


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0.0, "<10ms"),
        (0.009, "<10ms"),
        (0.010, "10-100ms"),
        (0.099, "10-100ms"),
        (0.100, "100ms-1s"),
        (0.999, "100ms-1s"),
        (1.000, "1-10s"),
        (9.999, "1-10s"),
        (10.000, ">10s"),
        (120.0, ">10s"),
    ],
)
def test_duration_bucket_boundaries(elapsed: float, expected: str):
    assert bucket(elapsed) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "<10KB"),
        (9_999, "<10KB"),
        (10_000, "10-100KB"),
        (99_999, "10-100KB"),
        (100_000, "100KB-1MB"),
        (999_999, "100KB-1MB"),
        (1_000_000, "1-10MB"),
        (9_999_999, "1-10MB"),
        (10_000_000, ">10MB"),
        (50_000_000, ">10MB"),
    ],
)
def test_byte_bucket_boundaries(size: int, expected: str):
    assert byte_bucket(size) == expected
