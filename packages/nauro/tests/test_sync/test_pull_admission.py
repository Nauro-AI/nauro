"""Legacy pull routes every manifest row through the sync path-admission layer."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import typer

from nauro.cli.commands.sync import _pull_via_presign, _unfinished_pull_detail
from nauro.constants import DECISIONS_DIR
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.sync import merge as merge_module
from nauro.sync import pull as pull_module
from nauro.sync._path_diagnostics import (
    _escape_path_for_display,
    _StoreRootPreparationError,
)
from nauro.sync.collisions import DecisionOutcome, DecisionVerdict
from nauro.sync.corpus import _UNUSABLE_DECISIONS, DecisionCorpus
from nauro.sync.hooks import pull_before_session
from nauro.sync.merge import CONFLICT_BACKUP_DIR
from nauro.sync.pull import PullReport, _Transfer, run_pull
from nauro.sync.state import SYNC_STATE_FILE, FileState, SyncState, load_state, save_state
from tests.test_sync.conftest import (
    CLOUD_PID,
    _manifest,
    _presign,
    _RecordingReporter,
    _scaffolded_cloud_project,
    _seed_token,
    decision_bytes,
    entry_names,
    pull_report,
    track,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
LINK_KINDS = ["symlink", "junction"]

ROOT_TEXT = "The Store root is unavailable."
OUTSIDE_TEXT = "The path resolves outside the Store root."
NOTE = "context/note.md"
DECISION_ROW = f"{DECISIONS_DIR}/003-x.md"
UNUSABLE = pull_module._DECISIONS_UNUSABLE_TEXT
AUTHORITY = f"{_REPLICA_CONTROL_ROOT_NAME}/authority.json"

RESERVED_ROWS = [
    "submission-records/record.json",
    "context/SUBMISSION-RECORDS/record.json",
    " submission-records. /record.json",
    "submission-records:stream/record.json",
    "submission-records\\record.json",
    AUTHORITY,
    _REPLICA_CONTROL_LOCK_NAME,
    ".REPLICA/x",
    " .replica. /x",
    ".replica:ads/x",
    ".replica\\x",
    f"{_REPLICA_CONTROL_LOCK_NAME}/child",
]

UNSAFE_ROWS = [
    ("C:\\x", "The path uses drive syntax."),
    ("\\\\server\\share\\x", "The path uses UNC syntax."),
    ("context/ :stream", "A path component normalizes to empty."),
    ("context/nul\x00name", "Path metadata is unavailable."),
]


@pytest.fixture()
def store(tmp_path):
    """A scaffolded cloud-mode store with a token, ready for ``run_pull``."""
    store = _scaffolded_cloud_project("pulladmission", tmp_path, project_id=CLOUD_PID)
    _seed_token()
    (store / "context").mkdir(exist_ok=True)
    return store


def _run(store, entries, *, reporter=None, etags=None):
    """Run one pull, also returning the paths it asked the server to presign."""
    operations: list[str] = []
    real = pull_module.request_presigned_urls

    def spy(project_id, ops, *, session=None):
        operations.extend(op["path"] for op in ops)
        return real(project_id, ops, session=session)

    with patch.object(pull_module, "request_presigned_urls", spy):
        report, reporter = pull_report(store, entries, reporter=reporter, etags=etags)
    return report, reporter, operations


def _md5_etag(body: bytes) -> str:
    """The S3-style ETag of ``body``, which makes a local copy an adoption."""
    return f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'


def _control_entries(store) -> set[str]:
    return {name for name in entry_names(store) if name.casefold().startswith(".replica")}


def _is_reserved(value) -> bool:
    for part in str(value).replace("\\", "/").split("/"):
        folded = part.split(":", 1)[0].strip(" ").rstrip(" .").casefold()
        if folded in {_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME}:
            return True
    return False


def _strand(path: Path, *, age_seconds: float = 600.0) -> Path:
    """Leave a tmp sibling of ``path`` behind, as a killed write would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    orphan = path.parent / f".{path.name}.{'ab' * 8}.tmp"
    orphan.write_bytes(b"half a write\n")
    stranded_at = time.time() - age_seconds
    os.utime(orphan, (stranded_at, stranded_at))
    return orphan


def _plant(store: Path, rel: str, body: bytes) -> None:
    """Leave untracked local content, so a differing remote row conflicts."""
    path = store / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _forbid_target(monkeypatch, target: Path) -> None:
    """Fail the test if ``target`` is read or written."""
    real_read = Path.read_bytes
    real_write = pull_module.atomic_write_bytes
    resolved = os.path.realpath(target)

    def read_bytes(self):
        if os.path.realpath(self) == resolved:
            pytest.fail(f"read {self}")
        return real_read(self)

    def write(path, content):
        if os.path.realpath(path) == resolved:
            pytest.fail(f"wrote {path}")
        return real_write(path, content)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(pull_module, "atomic_write_bytes", write)


def _link(link: Path, target: Path, kind: str) -> None:
    """Point ``link`` at ``target`` as a symlink or as a Windows junction."""
    if kind == "junction":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
        return
    link.symlink_to(target, target_is_directory=True)


def _require_link_kind(kind: str) -> None:
    if (kind == "junction") != (sys.platform == "win32"):
        pytest.skip(f"{kind} is not this platform's directory link")


def _link_decisions(store: Path, target: str, kind: str = "symlink") -> None:
    """Replace ``decisions/`` with a directory link at ``target``."""
    shutil.rmtree(store / DECISIONS_DIR)
    _link(store / DECISIONS_DIR, store / target, kind)


def _alias_decisions(store: Path, kind: str = "symlink") -> Path:
    """Link ``decisions/`` at a directory no other manifest row resolves into."""
    aliased = store / "aliased"
    aliased.mkdir()
    _link_decisions(store, "aliased", kind)
    return aliased


def _fail(name: str):
    """A stand-in that fails the test if the pull calls ``name``."""

    def refuse(*args, **kwargs):
        pytest.fail(f"called {name}")

    return refuse


@contextmanager
def _fake_server(entries):
    """Serve exactly ``entries`` over the presign transport."""
    bodies = dict(entries)
    manifest = _manifest(
        [
            {"path": rel, "etag": f'"{rel}-v1"', "size": len(body), "last_modified": "x"}
            for rel, body in entries
        ]
    )
    presigned = _presign([{"verb": "GET", "path": rel} for rel, _body in entries])

    def fake_get(url, **kwargs):
        if "/sync/manifest" in url:
            return manifest
        return httpx.Response(200, content=bodies[url.split("/GET/", 1)[1]])

    with (
        patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
        patch("nauro.sync.remote.httpx.Client.post", return_value=presigned),
    ):
        yield


@pytest.fixture()
def forbid_control_access(monkeypatch):
    """Fail the test if anything reaches a reserved control path."""
    real_lstat = merge_module.os.lstat
    real_stat = Path.stat
    real_read = Path.read_bytes
    real_compare = pull_module.compare_local_file
    real_resolve = pull_module.resolve_destination

    def check(value) -> None:
        if _is_reserved(value):
            pytest.fail(f"reached {value!r}")

    def lstat(path, *args, **kwargs):
        check(path)
        return real_lstat(path, *args, **kwargs)

    def stat(self, *args, **kwargs):
        check(self)
        return real_stat(self, *args, **kwargs)

    def read_bytes(self):
        check(self)
        return real_read(self)

    def compare(local_file, etag):
        check(local_file)
        return real_compare(local_file, etag)

    def resolve(store_path, rel):
        check(rel)
        return real_resolve(store_path, rel)

    monkeypatch.setattr(merge_module.os, "lstat", lstat)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(pull_module, "compare_local_file", compare)
    monkeypatch.setattr(pull_module, "resolve_destination", resolve)


# --- manifest table ---


def test_reserved_manifest_rows_install_nothing_and_say_nothing(store):
    entries = [(NOTE, b"note\n")] + [(row, b"control\n") for row in RESERVED_ROWS]

    report, reporter, operations = _run(store, entries)

    assert report == PullReport(merged=1)
    assert operations == [NOTE]
    assert (store / NOTE).read_bytes() == b"note\n"
    assert _control_entries(store) == set()
    assert set(load_state(store).files) == {NOTE}
    assert reporter.warns == []


@pytest.mark.parametrize("row, reason", UNSAFE_ROWS)
def test_unsafe_manifest_rows_are_refused_before_the_network(store, row, reason):
    report, reporter, operations = _run(store, [(NOTE, b"note\n"), (row, b"remote\n")])

    assert report == PullReport(merged=1, skipped_permanent=1)
    assert operations == [NOTE]
    assert reporter.warns == [f"skipping manifest entry {_escape_path_for_display(row)}: {reason}"]
    assert (store / NOTE).read_bytes() == b"note\n"
    assert set(load_state(store).files) == {NOTE}


def test_a_rooted_row_is_still_refused_by_the_traversal_guard(store):
    report, reporter, operations = _run(store, [(NOTE, b"note\n"), ("//?/C:/x", b"remote\n")])

    assert report == PullReport(merged=1, skipped_permanent=1)
    assert operations == [NOTE]
    assert any("suspicious manifest entry" in warning for warning in reporter.warns)


def test_the_refusal_names_the_escaped_display_form(store):
    _report, reporter, _operations = _run(store, [("context/ :stream", b"remote\n")])

    assert reporter.warns == [
        "skipping manifest entry context/\\x20:stream: A path component normalizes to empty."
    ]


# --- access-order table ---


def test_no_reserved_path_is_named_probed_or_read_while_routing(store, forbid_control_access):
    entries = [(NOTE, b"note\n")] + [(row, b"control\n") for row in RESERVED_ROWS]

    report, reporter, operations = _run(store, entries)

    assert report == PullReport(merged=1)
    assert operations == [NOTE]
    assert reporter.warns == []


def test_the_sweep_never_walks_the_store_with_rglob(store, monkeypatch):
    def refuse(self, *args, **kwargs):
        pytest.fail(f"rglob on {self}")

    monkeypatch.setattr(pull_module.Path, "rglob", refuse)

    report, _reporter, _operations = _run(store, [(NOTE, b"note\n")])

    assert report == PullReport(merged=1)


# --- link table ---


@POSIX_ONLY
def test_a_link_aliasing_the_control_root_installs_nothing(store):
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    (control / "authority.json").write_text("{}", encoding="utf-8")
    (store / "alias").symlink_to(_REPLICA_CONTROL_ROOT_NAME)

    report, reporter, operations = _run(store, [("alias/authority.json", b"remote\n")])

    assert report == PullReport()
    assert operations == []
    assert reporter.warns == []
    assert (control / "authority.json").read_text(encoding="utf-8") == "{}"
    assert load_state(store).files == {}


@POSIX_ONLY
def test_a_link_out_of_the_store_is_refused_with_the_fixed_reason(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"untouched\n")
    (store / "escape").symlink_to(outside)

    report, reporter, operations = _run(store, [("escape/victim.md", b"overwritten\n")])

    assert report == PullReport(skipped_permanent=1)
    assert operations == []
    assert reporter.warns == [f"skipping manifest entry escape/victim.md: {OUTSIDE_TEXT}"]
    assert (outside / "victim.md").read_bytes() == b"untouched\n"


@POSIX_ONLY
def test_the_write_barrier_refuses_a_late_control_link_silently(store):
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / "late").symlink_to(_REPLICA_CONTROL_ROOT_NAME)
    reporter = _RecordingReporter()

    allowed = pull_module._generic_write_allowed(
        store,
        _Transfer(rel="late/authority.json", etag='"e"', _body=b"remote\n"),
        SyncState(),
        reporter,
    )

    assert allowed is False
    assert reporter.warns == []
    assert entry_names(store / _REPLICA_CONTROL_ROOT_NAME) == set()


@POSIX_ONLY
def test_a_backup_directory_linked_at_the_control_root_refuses_the_run(store):
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / CONFLICT_BACKUP_DIR).symlink_to(_REPLICA_CONTROL_ROOT_NAME)
    entries = [(NOTE, b"note\n"), ("decisions/020-remote.md", decision_bytes(20, "Remote"))]

    report, reporter, operations = _run(store, entries)

    assert report == PullReport(refused=2)
    assert operations == []
    assert reporter.warns == ["pull skipped: the conflict backup directory is not usable"]
    assert not (store / NOTE).exists()
    assert entry_names(store / _REPLICA_CONTROL_ROOT_NAME) == set()


@POSIX_ONLY
def test_a_backup_directory_linked_outside_the_store_names_the_reason(store):
    (store / CONFLICT_BACKUP_DIR).symlink_to(Path("..") / "outside")
    entries = [(NOTE, b"note\n"), ("context/other.md", b"other\n")]

    report, reporter, operations = _run(store, entries)

    assert report == PullReport(refused=2)
    assert operations == []
    assert reporter.warns == [
        f"pull skipped: the conflict backup directory is not usable ({OUTSIDE_TEXT})"
    ]
    assert not (store / NOTE).exists()


def test_a_backup_directory_that_is_a_regular_file_refuses_the_run(store):
    (store / CONFLICT_BACKUP_DIR).write_text("not a directory", encoding="utf-8")

    report, reporter, operations = _run(store, [(NOTE, b"note\n")])

    assert report == PullReport(refused=1)
    assert operations == []
    assert reporter.warns == ["pull skipped: the conflict backup directory is not usable"]
    assert not (store / NOTE).exists()


# --- destination table ---


def test_an_existing_directory_keeps_the_legacy_refusal(store):
    (store / "context" / "folder").mkdir(parents=True)

    report, reporter, operations = _run(store, [("context/folder", b"remote\n")])

    assert report == PullReport(skipped_permanent=1)
    assert operations == []
    assert any("it resolves to the directory" in warning for warning in reporter.warns)


@POSIX_ONLY
def test_a_special_file_destination_is_refused_and_never_hashed(store, monkeypatch):
    os.mkfifo(store / "context" / "pipe.md")

    def refuse(local_file, etag):
        pytest.fail(f"hashed {local_file}")

    monkeypatch.setattr(pull_module, "compare_local_file", refuse)

    report, reporter, operations = _run(store, [("context/pipe.md", b"remote\n")])

    assert report == PullReport(skipped_permanent=1)
    assert operations == []
    assert reporter.warns == [
        "skipping manifest entry context/pipe.md: it resolves to a special file"
    ]


# --- sweep table ---


def test_stale_tmp_siblings_under_the_store_and_the_backups_are_removed(store):
    in_store = _strand(store / "stack.md")
    in_backups = _strand(store / CONFLICT_BACKUP_DIR / "losing-side.md")

    _report, reporter, _operations = _run(store, [])

    assert not in_store.exists()
    assert not in_backups.exists()
    assert "Cleaned 2 interrupted write(s)" in reporter.infos


def test_a_stale_tmp_sibling_under_the_control_root_survives_unread(store, forbid_control_access):
    orphan = _strand(store / _REPLICA_CONTROL_ROOT_NAME / "authority.json")

    _report, reporter, _operations = _run(store, [])

    assert os.listdir(store / _REPLICA_CONTROL_ROOT_NAME) == [orphan.name]
    assert not any("interrupted write" in line for line in reporter.infos)


@POSIX_ONLY
def test_a_tmp_sibling_named_link_into_the_control_root_survives_unread(
    store, forbid_control_access
):
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    target = control / "authority.json"
    target.write_text("{}", encoding="utf-8")
    link = store / f".authority.json.{'ab' * 8}.tmp"
    link.symlink_to(Path(_REPLICA_CONTROL_ROOT_NAME) / "authority.json")

    _report, reporter, _operations = _run(store, [])

    assert link.name in os.listdir(store)
    assert os.readlink(link) == os.path.join(_REPLICA_CONTROL_ROOT_NAME, "authority.json")
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "{}"
    assert not any("interrupted write" in line for line in reporter.infos)


def test_an_unscannable_subdirectory_is_skipped_without_a_warning(store):
    locked = store / "context" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0o000)
    try:
        report, reporter, _operations = _run(store, [(NOTE, b"note\n")])
    finally:
        locked.chmod(0o755)

    assert report == PullReport(merged=1)
    assert reporter.warns == []


# --- root table ---


def test_a_missing_root_is_refused_before_any_lock(tmp_path):
    missing = tmp_path / "gone"

    with pytest.raises(_StoreRootPreparationError) as raised:
        run_pull(CLOUD_PID, missing, _RecordingReporter())

    assert str(raised.value) == ROOT_TEXT
    assert not missing.exists()


def test_a_regular_file_root_is_refused_before_any_lock(tmp_path):
    root = tmp_path / "file-root"
    root.write_text("store", encoding="utf-8")

    with pytest.raises(_StoreRootPreparationError):
        run_pull(CLOUD_PID, root, _RecordingReporter())

    assert root.read_text(encoding="utf-8") == "store"
    assert list(tmp_path.iterdir()) == [root]


def test_the_cli_pull_reports_the_typed_refusal_and_exits_one(tmp_path, capsys):
    missing = tmp_path / "gone"

    with pytest.raises(typer.Exit) as raised:
        _pull_via_presign(CLOUD_PID, missing)

    assert raised.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == f"Error: pull skipped ({ROOT_TEXT})\n"
    assert str(missing) not in captured.out + captured.err


def test_the_session_hook_logs_the_typed_refusal_and_returns_zero(tmp_path, caplog):
    _scaffolded_cloud_project("pullhook", tmp_path, project_id=CLOUD_PID)
    _seed_token()
    missing = tmp_path / "gone"

    with caplog.at_level(logging.WARNING, logger="nauro.sync"):
        assert pull_before_session(CLOUD_PID, missing) == 0

    assert [record.getMessage() for record in caplog.records] == [f"sync pull: {ROOT_TEXT}"]
    assert all(record.exc_info is None for record in caplog.records)
    assert str(missing) not in caplog.text


# --- state table ---


def test_a_sync_state_entry_under_a_reserved_path_is_left_untouched(store):
    seeded = FileState(
        local_sha256="a" * 64, remote_etag='"seeded"', last_sync="2026-08-10T00:00:00Z"
    )
    state = load_state(store)
    state.files[AUTHORITY] = seeded
    save_state(store, state)
    before = json.loads((store / SYNC_STATE_FILE).read_text(encoding="utf-8"))["files"][AUTHORITY]

    report, _reporter, _operations = _run(store, [(NOTE, b"note\n"), (AUTHORITY, b"remote\n")])

    after = json.loads((store / SYNC_STATE_FILE).read_text(encoding="utf-8"))["files"]
    assert report == PullReport(merged=1)
    assert after[AUTHORITY] == before
    assert set(after) == {AUTHORITY, NOTE}


# --- conflict-backup naming table ---


CONFLICTS = {
    "state\\history.md": b"local history\n",
    "context\\victim.md": b"local victim\n",
    "context_victim.md": b"local sibling\n",
}


def test_conflicting_rows_back_up_under_distinct_flat_names(store):
    for rel, body in CONFLICTS.items():
        _plant(store, rel, body)
    before = entry_names(store)

    report, _reporter, _operations = _run(
        store, [(rel, b"remote " + body) for rel, body in CONFLICTS.items()]
    )

    backups = list((store / CONFLICT_BACKUP_DIR).iterdir())
    assert report == PullReport(merged=3)
    assert len({path.name for path in backups}) == 3
    assert all(path.is_file() for path in backups)
    assert not any("\\" in path.name or "/" in path.name for path in backups)
    assert sorted(path.read_bytes() for path in backups) == sorted(CONFLICTS.values())
    # A set-union merge writes no backup and keeps both sides, so the remote
    # bytes alone prove the union policy was not selected for this row.
    assert (store / "state\\history.md").read_bytes() == b"remote local history\n"
    assert entry_names(store) - before <= {
        CONFLICT_BACKUP_DIR,
        SYNC_STATE_FILE,
        f"{SYNC_STATE_FILE}.lock",
    }


RESOLVER_ROWS = {
    "context/victim.md": b"local victim\n",
    "context_victim.md": b"local sibling\n",
}


def test_a_nested_row_and_its_flat_sibling_back_up_under_distinct_names(store, caplog):
    for rel, body in RESOLVER_ROWS.items():
        _plant(store, rel, body)

    with caplog.at_level(logging.WARNING, logger="nauro.sync"):
        report, _reporter, _operations = _run(
            store, [(rel, b"remote " + body) for rel, body in RESOLVER_ROWS.items()]
        )

    backups = list((store / CONFLICT_BACKUP_DIR).iterdir())
    assert report == PullReport(merged=2)
    assert len({path.name for path in backups}) == 2
    assert all(path.is_file() for path in backups)
    assert not any("\\" in path.name or "/" in path.name for path in backups)
    assert sorted(path.read_bytes() for path in backups) == sorted(RESOLVER_ROWS.values())
    assert [
        record.getMessage()
        for record in caplog.records
        if "context/victim.md" in record.getMessage()
    ]


def test_an_append_only_conflict_still_set_unions_without_a_backup(store):
    rel = "state_history.md"
    _plant(store, rel, b"## History\n\nlocal entry\n")

    report, _reporter, _operations = _run(store, [(rel, b"## History\n\nremote entry\n")])

    merged = (store / rel).read_bytes()
    assert report == PullReport(merged=1)
    assert b"local entry" in merged and b"remote entry" in merged
    assert not (store / CONFLICT_BACKUP_DIR).exists()


# --- decision write-barrier table ---


@POSIX_ONLY
def test_a_decision_destination_swapped_for_a_control_link_is_dropped_silently(store, monkeypatch):
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    target = control / "authority.json"
    target.write_text("{}", encoding="utf-8")
    rel = "decisions/900-remote.md"
    corpus = DecisionCorpus.scan(store)
    (store / rel).symlink_to(Path("..") / _REPLICA_CONTROL_ROOT_NAME / "authority.json")
    _forbid_target(monkeypatch, target)
    reporter = _RecordingReporter()
    tally = pull_module._Tally()

    pull_module._apply_decision(
        corpus,
        _Transfer(rel=rel, etag='"e"', _body=decision_bytes(900, "Remote")),
        SyncState(),
        pull_module._Manifest((), frozenset(), frozenset()),
        reporter,
        tally,
    )

    assert tally.skipped_permanent == 1
    assert tally.merged == 0
    assert reporter.warns == []
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "{}"


@POSIX_ONLY
def test_a_decision_destination_swapped_for_an_outside_link_is_refused(
    store, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "victim.md"
    target.write_bytes(b"untouched\n")
    rel = "decisions/901-remote.md"
    (store / rel).symlink_to(target)
    monkeypatch.setattr(
        pull_module,
        "classify_decision",
        lambda *args: DecisionVerdict(DecisionOutcome.resolve_conflict, (store / rel,)),
    )
    _forbid_target(monkeypatch, target)
    reporter = _RecordingReporter()
    tally = pull_module._Tally()

    pull_module._apply_decision(
        DecisionCorpus.scan(store),
        _Transfer(rel=rel, etag='"e"', _body=decision_bytes(901, "Remote")),
        SyncState(),
        pull_module._Manifest((), frozenset(), frozenset()),
        reporter,
        tally,
    )

    assert tally.skipped_permanent == 1
    assert tally.merged == 0
    assert reporter.warns == [f"refusing to write {rel}: {OUTSIDE_TEXT}"]
    with open(target, "rb") as handle:
        assert handle.read() == b"untouched\n"


# --- decision-gating table ---


GATED_ROWS = [(NOTE, b"note\n"), (DECISION_ROW, decision_bytes(3, "Remote"))]


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_decisions_link_over_context_refuses_every_row_it_aliases(store, kind):
    _require_link_kind(kind)
    _link_decisions(store, "context", kind)

    report, reporter, operations = _run(store, GATED_ROWS)

    # The link puts context/ inside the decisions root, so the generic row is
    # gated too: both rows are decision work this run refuses.
    assert report == PullReport(refused=2)
    assert report.left_work_behind is True
    assert reporter.warns == [UNUSABLE]
    assert operations == []
    assert entry_names(store / "context") == set()
    assert set(load_state(store).files) == set()


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_linked_decisions_directory_refuses_the_decision_row_alone(store, kind):
    _require_link_kind(kind)
    aliased = _alias_decisions(store, kind)

    report, reporter, operations = _run(store, GATED_ROWS)

    assert report == PullReport(merged=1, refused=1)
    assert report.left_work_behind is True
    assert reporter.warns == [UNUSABLE]
    assert operations == [NOTE]
    assert entry_names(aliased) == set()
    assert entry_names(store / "context") == {"note.md"}
    assert set(load_state(store).files) == {NOTE}


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_tracked_decision_is_refused_through_a_linked_decisions_directory(store, kind):
    _require_link_kind(kind)
    aliased = _alias_decisions(store, kind)
    local = decision_bytes(3, "Local")
    (aliased / "003-x.md").write_bytes(local)
    track(store, DECISION_ROW)

    monkeypatch = pytest.MonkeyPatch()
    try:
        _forbid_target(monkeypatch, aliased / "003-x.md")
        monkeypatch.setattr(pull_module, "compare_local_file", _fail("compare_local_file"))
        monkeypatch.setattr(pull_module, "file_changed_locally", _fail("file_changed_locally"))
        report, reporter, operations = _run(store, GATED_ROWS)
    finally:
        monkeypatch.undo()

    # Tracked and present, so the decision gate would exempt the row and route
    # it as an install; the domain check refuses it before the reads that
    # decision would need.
    assert report == PullReport(merged=1, refused=1)
    assert reporter.warns == [UNUSABLE]
    assert operations == [NOTE]
    assert entry_names(aliased) == {"003-x.md"}
    assert (aliased / "003-x.md").read_bytes() == local
    assert set(load_state(store).files) == {NOTE, DECISION_ROW}


@POSIX_ONLY
def test_unchanged_tracked_decisions_still_count_as_work_left_behind(store):
    aliased = _alias_decisions(store)
    rows = [(DECISION_ROW, decision_bytes(3, "Same")), (f"{DECISIONS_DIR}/004-y.md", b"same\n")]
    for rel, body in rows:
        (aliased / rel.partition("/")[2]).write_bytes(body)
        track(store, rel)
    stamp_before = load_state(store).last_full_sync

    # Every etag matches sync state, so each row would be ignored as unchanged;
    # an unusable decisions/ still owes them to the next run.
    report, reporter, operations = _run(store, rows, etags={rel: '"pushed"' for rel, _ in rows})

    assert report == PullReport(refused=2)
    assert report.left_work_behind is True
    assert reporter.warns == [UNUSABLE]
    assert operations == []
    assert load_state(store).last_full_sync == stamp_before


@POSIX_ONLY
def test_a_decision_row_unsafe_by_its_own_spelling_stays_permanently_skipped(store):
    _alias_decisions(store)
    hostile = f"{DECISIONS_DIR}/ :stream"

    report, reporter, operations = _run(store, [(NOTE, b"note\n"), (hostile, b"x\n")])

    # The folded-empty component can never install, repaired directory or not,
    # so it is not work left behind.
    assert report == PullReport(merged=1, skipped_permanent=1)
    assert reporter.warns.count(UNUSABLE) == 1
    assert operations == [NOTE]


def test_a_regular_file_at_the_decisions_directory_refuses_the_decision_row(store):
    shutil.rmtree(store / DECISIONS_DIR)
    (store / DECISIONS_DIR).write_text("not a directory", encoding="utf-8")
    stamp_before = load_state(store).last_full_sync

    report, reporter, operations = _run(store, GATED_ROWS)

    # Refused, not permanently skipped: the row installs once decisions/ is
    # repaired, so the run leaves work behind and does not stamp a full sync.
    assert report == PullReport(merged=1, refused=1)
    assert report.left_work_behind is True
    assert reporter.warns.count(UNUSABLE) == 1
    assert operations == [NOTE]
    assert (store / DECISIONS_DIR).read_text(encoding="utf-8") == "not a directory"
    assert set(load_state(store).files) == {NOTE}
    assert load_state(store).last_full_sync == stamp_before


@POSIX_ONLY
def test_decisions_linked_at_the_control_root_refuses_decision_work(store):
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    (control / "authority.json").write_text("{}", encoding="utf-8")
    _link_decisions(store, _REPLICA_CONTROL_ROOT_NAME)

    report, reporter, operations = _run(store, GATED_ROWS)

    assert report == PullReport(merged=1)
    assert reporter.warns == [UNUSABLE]
    assert operations == [NOTE]
    assert entry_names(control) == {"authority.json"}
    assert (store / NOTE).read_bytes() == b"note\n"


# --- absent-decisions table ---


def test_an_absent_decisions_directory_still_installs_a_decision(store):
    shutil.rmtree(store / DECISIONS_DIR)

    report, reporter, operations = _run(store, [(DECISION_ROW, decision_bytes(3, "Remote"))])

    assert report == PullReport(merged=1)
    assert reporter.warns == []
    assert operations == [DECISION_ROW]
    assert (store / DECISIONS_DIR).is_dir()
    assert entry_names(store / DECISIONS_DIR) - {".lock"} == {"003-x.md"}


# --- gated access-order table ---


@POSIX_ONLY
def test_nothing_locks_classifies_or_writes_through_a_linked_decisions_directory(
    store, monkeypatch
):
    aliased = _alias_decisions(store)
    _forbid_target(monkeypatch, aliased / "003-x.md")
    monkeypatch.setattr(pull_module, "decision_lock", _fail("decision_lock"))
    monkeypatch.setattr(pull_module, "run_completion_pass", _fail("run_completion_pass"))
    monkeypatch.setattr(pull_module, "classify_decision", _fail("classify_decision"))

    report, reporter, operations = _run(store, GATED_ROWS)

    assert report == PullReport(merged=1, refused=1)
    assert reporter.warns == [UNUSABLE]
    assert operations == [NOTE]
    assert entry_names(aliased) == set()
    assert entry_names(store / "context") == {"note.md"}


# --- rescan table ---


def test_a_decisions_directory_unusable_inside_the_lock_is_refused_there(store, monkeypatch):
    real_scan = DecisionCorpus.scan
    scans: list[Path] = []

    class _Rescanned:
        @staticmethod
        def scan(store_path):
            corpus = real_scan(store_path)
            scans.append(store_path)
            if len(scans) > 1:
                corpus.usable = False
            return corpus

    monkeypatch.setattr(pull_module, "DecisionCorpus", _Rescanned)
    monkeypatch.setattr(pull_module, "run_completion_pass", _fail("run_completion_pass"))
    monkeypatch.setattr(pull_module, "classify_decision", _fail("classify_decision"))

    report, reporter, operations = _run(store, GATED_ROWS)

    assert report == PullReport(merged=1, refused=1)
    assert reporter.warns == [UNUSABLE]
    assert operations == [DECISION_ROW, NOTE]
    assert entry_names(store / DECISIONS_DIR) - {".lock"} == {"001-initial-setup.md"}
    assert set(load_state(store).files) == {NOTE}


def _rescan_unusable(monkeypatch, *, before_rescan=None):
    """Make the in-lock corpus scan report an unusable decisions/."""
    real_scan = DecisionCorpus.scan
    scans: list[Path] = []

    class _Rescanned:
        @staticmethod
        def scan(store_path):
            scans.append(store_path)
            if len(scans) > 1 and before_rescan is not None:
                before_rescan()
            corpus = real_scan(store_path)
            if len(scans) > 1:
                corpus.usable = False
            return corpus

    monkeypatch.setattr(pull_module, "DecisionCorpus", _Rescanned)
    monkeypatch.setattr(pull_module, "run_completion_pass", _fail("run_completion_pass"))


@pytest.mark.parametrize("queue", ["pulls", "conflicts", "adopted"])
def test_a_tracked_decision_in_any_queue_is_refused_by_the_rescan(store, monkeypatch, queue):
    local = decision_bytes(3, "Local")
    (store / DECISION_ROW).write_bytes(local)
    track(store, DECISION_ROW)
    remote = decision_bytes(3, "Remote")
    etags = {}
    if queue == "conflicts":
        (store / DECISION_ROW).write_bytes(decision_bytes(3, "Edited"))
    elif queue == "adopted":
        remote = local
        etags[DECISION_ROW] = _md5_etag(local)
    _rescan_unusable(monkeypatch)

    report, reporter, operations = _run(
        store, [(NOTE, b"note\n"), (DECISION_ROW, remote)], etags=etags
    )

    # Queued as a plain install, a conflict, or an adoption at triage; the
    # rescan pulls it back out of that queue before anything is written or
    # recorded.
    assert report == PullReport(merged=1, refused=1)
    assert reporter.warns == [UNUSABLE]
    assert operations == ([NOTE] if queue == "adopted" else [NOTE, DECISION_ROW])
    assert (store / DECISION_ROW).read_bytes() == (
        decision_bytes(3, "Edited") if queue == "conflicts" else local
    )
    assert load_state(store).files[DECISION_ROW].remote_etag == '"pushed"'
    assert set(load_state(store).files) == {NOTE, DECISION_ROW}


@POSIX_ONLY
def test_unchanged_decisions_count_when_the_directory_is_replaced_before_the_lock(
    store, monkeypatch
):
    rows = [(DECISION_ROW, decision_bytes(3, "Same")), (f"{DECISIONS_DIR}/004-y.md", b"same\n")]
    for rel, body in rows:
        (store / rel).write_bytes(body)
        track(store, rel)
    stamp_before = load_state(store).last_full_sync

    def plant():
        shutil.rmtree(store / DECISIONS_DIR)
        (store / "empty").mkdir()
        (store / DECISIONS_DIR).symlink_to("empty", target_is_directory=True)

    _rescan_unusable(monkeypatch, before_rescan=plant)

    report, reporter, operations = _run(store, rows, etags={rel: '"pushed"' for rel, _ in rows})

    # Both rows were ignored as unchanged at triage and so held nothing to drop;
    # the counter still owes them to the next run, so no full sync is stamped
    # over decisions that are no longer present locally.
    assert report == PullReport(refused=2)
    assert report.left_work_behind is True
    assert reporter.warns == [UNUSABLE]
    assert operations == []
    assert entry_names(store / "empty") == set()
    assert set(load_state(store).files) == {rel for rel, _ in rows}
    assert load_state(store).last_full_sync == stamp_before


def test_unchanged_decisions_leave_a_usable_run_exactly_as_before(store):
    rows = [(DECISION_ROW, decision_bytes(3, "Same")), (f"{DECISIONS_DIR}/004-y.md", b"same\n")]
    for rel, body in rows:
        (store / rel).write_bytes(body)
        track(store, rel)
    stamp_before = load_state(store).last_full_sync

    report, reporter, operations = _run(store, rows, etags={rel: '"pushed"' for rel, _ in rows})

    assert report == PullReport()
    assert report.left_work_behind is False
    assert reporter.warns == []
    assert operations == []
    stamp_after = load_state(store).last_full_sync
    assert stamp_after and stamp_after != stamp_before


@POSIX_ONLY
def test_a_link_planted_between_triage_and_the_lock_drops_the_rows_it_now_covers(
    store, monkeypatch
):
    stamp_before = load_state(store).last_full_sync

    def plant():
        shutil.rmtree(store / DECISIONS_DIR)
        (store / DECISIONS_DIR).symlink_to("context", target_is_directory=True)

    _rescan_unusable(monkeypatch, before_rescan=plant)

    report, reporter, operations = _run(store, GATED_ROWS)

    # context/note.md was a plain pull at triage; now it resolves inside the
    # decisions root, so the rescan drops it with the gated row.
    assert report == PullReport(refused=2)
    assert report.left_work_behind is True
    assert reporter.warns == [UNUSABLE]
    assert operations == [DECISION_ROW, NOTE]
    assert entry_names(store / "context") == set()
    assert set(load_state(store).files) == set()
    assert load_state(store).last_full_sync == stamp_before


def test_a_landed_decision_is_recorded_when_a_later_classification_fails(store, monkeypatch):
    second = f"{DECISIONS_DIR}/004-y.md"
    real_classify = pull_module.classify_decision

    def classify(corpus, rel, content, state, paths):
        if rel == second:
            raise RuntimeError("classifier fault")
        return real_classify(corpus, rel, content, state, paths)

    monkeypatch.setattr(pull_module, "classify_decision", classify)
    rows = [(DECISION_ROW, decision_bytes(3, "Remote")), (second, decision_bytes(4, "Remote"))]

    with _fake_server(rows), pytest.raises(RuntimeError, match="classifier fault"):
        run_pull(CLOUD_PID, store, _RecordingReporter())

    # The first decision landed under the lock before the fault, so its state
    # is saved on the way out rather than lost with the run.
    assert (store / DECISION_ROW).read_bytes() == decision_bytes(3, "Remote")
    assert DECISION_ROW in load_state(store).files
    assert not (store / second).exists()


# --- gated surface table ---


@POSIX_ONLY
def test_the_cli_pull_reports_the_gated_decision_and_leaves_work_behind(store, capsys):
    _alias_decisions(store)

    with _fake_server(GATED_ROWS):
        report = _pull_via_presign(CLOUD_PID, store)

    captured = capsys.readouterr()
    assert report == PullReport(merged=1, refused=1)
    assert captured.err.count(f"  {UNUSABLE}\n") == 1
    assert "Left 1 item(s) for the next sync" in captured.out
    assert _unfinished_pull_detail(report) == "1 remote file(s) were not written this sync"


@POSIX_ONLY
def test_the_session_hook_returns_the_merged_count_and_logs_the_gate(store, caplog):
    aliased = _alias_decisions(store)

    with caplog.at_level(logging.WARNING, logger="nauro.sync"), _fake_server(GATED_ROWS):
        assert pull_before_session(CLOUD_PID, store) == 1

    # The corpus names the unusable directory once and the pull names the
    # skipped work once; nothing else reaches the log.
    assert [record.getMessage() for record in caplog.records] == [
        _UNUSABLE_DECISIONS,
        f"sync pull: {UNUSABLE}",
    ]
    assert entry_names(aliased) == set()
    assert entry_names(store / "context") == {"note.md"}
