"""Drift tripwire for ``sanitize_sub``.

These assertions pin the byte-for-byte contract of the per-user S3 key-prefix
derivation. The prefix is the storage isolation boundary, so any drift here
would route a user's writes and reads to different prefixes.
"""

import re
import string

import pytest

from nauro_core.identity import sanitize_sub


def test_allowed_class_round_trips_unchanged():
    allowed = string.ascii_letters + string.digits + "_-"
    assert sanitize_sub(allowed) == allowed


@pytest.mark.parametrize("sample", ["aZ09", "ABCxyz", "0123456789", "_-_-", "a_b-c"])
def test_allowed_samples_unchanged(sample):
    assert sanitize_sub(sample) == sample


def test_replacement_is_one_for_one_not_collapsed():
    assert sanitize_sub("a||b") == "a--b"


def test_replacement_uses_single_dash():
    assert sanitize_sub("user@example.com") == "user-example-com"


def test_cap_is_exactly_128_over_limit():
    assert len(sanitize_sub("x" * 199)) == 128


def test_cap_at_boundary():
    assert len(sanitize_sub("x" * 128)) == 128


def test_cap_under_boundary():
    assert len(sanitize_sub("x" * 127)) == 127


def test_real_world_auth0():
    assert sanitize_sub("auth0|abc123") == "auth0-abc123"


def test_real_world_google_oauth2():
    assert sanitize_sub("google-oauth2|456def") == "google-oauth2-456def"


def test_real_world_already_safe_unchanged():
    assert sanitize_sub("abc-def_123") == "abc-def_123"


def test_empty_string():
    assert sanitize_sub("") == ""


def test_adversarial_backslash_path_separator():
    assert sanitize_sub("auth0\\evil") == "auth0-evil"


def test_adversarial_relative_path_traversal():
    assert sanitize_sub("../../other") == "------other"


def test_adversarial_forward_slash():
    assert sanitize_sub("auth0/evil") == "auth0-evil"


def test_adversarial_unicode_each_char_replaced():
    assert sanitize_sub("café") == "caf-"


# Parity tripwire: prove the regex-free rewrite is byte-identical to the
# original regex over representative and adversarial inputs. The source stays
# regex-free; only this test references ``re``, to pin parity against the
# implementation it replaced.
PARITY_INPUTS = [
    "",
    "abc-def_123",
    "auth0|abc123",
    "google-oauth2|456def",
    "user@example.com",
    "a||b",
    "auth0\\evil",
    "../../other",
    "auth0/evil",
    "café",
    "spaces and tabs\there",
    "emoji \U0001f600 mix",
    "x" * 199,
    string.ascii_letters + string.digits + "_-",
    "MiXeD|cAsE@2026/05::29",
]


@pytest.mark.parametrize("sub", PARITY_INPUTS)
def test_parity_with_original_regex(sub):
    expected = re.sub(r"[^a-zA-Z0-9_\-]", "-", sub)[:128]
    assert sanitize_sub(sub) == expected
