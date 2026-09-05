"""Private submission records cannot enter raw sync."""

from pathlib import Path

import pytest

from nauro.sync import merge
from nauro.sync._path_diagnostics import _PathClass
from nauro.sync.push import plan_push
from nauro.sync.state import SyncState


@pytest.mark.parametrize(
    "path",
    [
        "submission-records",
        "./submission-records/record.json",
        "submission-records/record.json",
        "SUBMISSION-RECORDS/record.json",
        " submission-records. /record.json",
        "submission-records:stream/record.json",
        "submission-records\\record.json",
        "context/submission-records/record.json",
    ],
)
def test_record_aliases_are_reserved(path: str) -> None:
    assert merge._classify_sync_path(path).path_class is _PathClass.RESERVED_CONTROL
    assert merge.should_skip(path) is True


@pytest.mark.parametrize("path", ["context/note.md", "submission-records.md", "poetry.lock"])
def test_ordinary_files_remain_syncable(path: str) -> None:
    assert merge.should_skip(path) is False


def test_push_prunes_copied_records_and_links(tmp_path: Path) -> None:
    store = tmp_path / "store"
    records = store / "context" / "submission-records"
    records.mkdir(parents=True)
    (records / "record.json").write_text("private approval")
    (store / "note.md").write_text("public note")
    (store / "record-link.json").symlink_to(records / "record.json")
    plan = plan_push(store, SyncState())
    assert [item.relative_path for item in plan.candidates] == ["note.md"]
    assert plan.unsafe == ()
