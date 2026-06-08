"""Tests for the ``_json_input`` CLI helper.

The helper parses CLI arguments shaped as ``list[dict]`` from three input
sources: literal JSON on the command line, ``@path`` to a file, and ``-``
for stdin. It raises ``typer.BadParameter`` with the flag name on every
malformed input so Typer renders the error to stderr at exit 2 without
ever invoking the underlying adapter.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import typer

from nauro.cli._json_input import parse_json_list_of_dicts


def test_literal_json_list_of_dicts() -> None:
    parsed = parse_json_list_of_dicts(
        '[{"alternative": "X", "reason": "Y"}]',
        "--rejected",
    )
    assert parsed == [{"alternative": "X", "reason": "Y"}]


def test_at_sigil_reads_file(tmp_path: Path) -> None:
    payload = tmp_path / "rejected.json"
    payload.write_text('[{"alternative": "X", "reason": "Y"}]')
    parsed = parse_json_list_of_dicts(f"@{payload}", "--rejected")
    assert parsed == [{"alternative": "X", "reason": "Y"}]


def test_at_sigil_reads_utf8_file_regardless_of_locale(tmp_path: Path) -> None:
    # JSON is UTF-8 by spec, and rejected-alternative text flows into a decision
    # file on disk; a non-ASCII char (em-dash) must decode the same way no matter
    # what the platform's default encoding is, so the read pins UTF-8.
    payload = tmp_path / "rejected.json"
    payload.write_bytes(
        b'[{"alternative": "Redis", "reason": "single-AZ \xe2\x80\x94 no failover"}]'
    )
    parsed = parse_json_list_of_dicts(f"@{payload}", "--rejected")
    assert parsed == [{"alternative": "Redis", "reason": "single-AZ — no failover"}]


def test_stdin_sigil_reads_stdin(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('[{"alternative": "X", "reason": "Y"}]'),
    )
    parsed = parse_json_list_of_dicts("-", "--rejected")
    assert parsed == [{"alternative": "X", "reason": "Y"}]


def test_stdin_sigil_empty_input_rejects(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts("-", "--rejected")
    assert "--rejected" in str(excinfo.value)
    assert "stdin closed without input" in str(excinfo.value)


def test_at_sigil_missing_file_rejects(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts(f"@{missing}", "--rejected")
    msg = str(excinfo.value)
    assert "--rejected" in msg
    assert "does not exist" in msg
    assert str(missing) in msg


def test_malformed_json_rejects() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts("not json", "--rejected")
    msg = str(excinfo.value)
    assert "--rejected" in msg
    assert "invalid JSON" in msg


def test_json_object_instead_of_list_rejects() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts('{"not": "a list"}', "--rejected")
    msg = str(excinfo.value)
    assert "--rejected" in msg
    assert "expected JSON array of objects" in msg


def test_list_with_non_dict_element_rejects() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts('[{"a": 1}, "scalar"]', "--rejected")
    msg = str(excinfo.value)
    assert "--rejected" in msg
    assert "element [1] is not an object" in msg


def test_list_of_dicts_with_unusual_but_valid_keys() -> None:
    parsed = parse_json_list_of_dicts(
        '[{"a": 1, "nested": {"k": [1, 2]}, "": "empty-key"}]',
        "--rejected",
    )
    assert parsed == [{"a": 1, "nested": {"k": [1, 2]}, "": "empty-key"}]


def test_at_sigil_pointing_at_directory_rejects(tmp_path: Path) -> None:
    sub = tmp_path / "subdir"
    sub.mkdir()
    with pytest.raises(typer.BadParameter) as excinfo:
        parse_json_list_of_dicts(f"@{sub}", "--rejected")
    msg = str(excinfo.value)
    assert "--rejected" in msg
    assert "does not exist" in msg
    assert str(sub) in msg
