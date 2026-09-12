"""Local-file reads separate absent from unreadable, and never guess."""

from pathlib import Path

import pytest

from nauro.store.local_files import UnreadableFileError, is_regular_file, read_text_or_absent


def test_absent_file_or_ancestor_reads_as_none(tmp_path: Path):
    assert read_text_or_absent(tmp_path / "missing.md") is None
    assert read_text_or_absent(tmp_path / "missing-dir" / "missing.md") is None
    (tmp_path / "file-not-dir").write_text("x", encoding="utf-8")
    assert read_text_or_absent(tmp_path / "file-not-dir" / "missing.md") is None


def test_present_file_reads_its_text(tmp_path: Path):
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")
    assert read_text_or_absent(tmp_path / "a.md") == "hello"


def test_directory_in_place_of_a_file_is_unreadable_with_its_path(tmp_path: Path):
    (tmp_path / "a.md").mkdir()
    with pytest.raises(UnreadableFileError) as excinfo:
        read_text_or_absent(tmp_path / "a.md")
    assert excinfo.value.path == tmp_path / "a.md"
    assert excinfo.value.reason


def test_undecodable_bytes_are_unreadable_unless_read_leniently(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"\xff\xfe")
    with pytest.raises(UnreadableFileError, match="a.md"):
        read_text_or_absent(tmp_path / "a.md")
    assert read_text_or_absent(tmp_path / "a.md", errors="replace") == "\ufffd\ufffd"


def test_is_regular_file_answers_without_reading(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"\xff")
    assert is_regular_file(tmp_path / "a.md") is True
    assert is_regular_file(tmp_path / "missing.md") is False
    (tmp_path / "d").mkdir()
    assert is_regular_file(tmp_path / "d") is False
