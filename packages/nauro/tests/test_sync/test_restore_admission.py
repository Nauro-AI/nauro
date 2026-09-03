"""Cloud restore and the decision corpus route every path through admission."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD, PROJECT_MD
from nauro.store import recovery
from nauro.store.recovery import RecoveryError, restore_cloud_store
from nauro.store.registry import get_store_path_v2
from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.store.repo_config import save_repo_config
from nauro.sync import corpus as corpus_module
from nauro.sync import merge as merge_module
from nauro.sync._path_diagnostics import (
    _escape_path_for_display,
    _StoreRootPreparationError,
)
from nauro.sync.corpus import DecisionCorpus, IrregularEntry, SkipReason
from nauro.sync.remote import FetchedObject
from nauro.sync.state import SYNC_STATE_FILE
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_sync.conftest import (
    CLOUD_PID,
    _RecordingReporter,
    _scaffolded_cloud_project,
    _seed_token,
    decision_bytes,
    entry_names,
    pull_report,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
LINK_KINDS = ["symlink", "junction"]

PID = "01KQ6AZGNA0B3QBF67NBXP3S45"
NOTE = "context/note.md"
CONTROL_DISCARDED = "The restore staging area held local control state; it was discarded."
STAGING_UNAVAILABLE = "The restore staging area is unavailable."
UNUSABLE_DECISIONS = "The decisions directory is not usable this run."

RESERVED_ROWS = [
    f"{_REPLICA_CONTROL_ROOT_NAME}/authority.json",
    _REPLICA_CONTROL_LOCK_NAME,
    ".REPLICA/x",
    " .replica. /x",
    ".replica:ads/x",
]

UNSAFE_ROWS = {
    "C:x": "The path uses drive syntax.",
    "context/ :stream": "A path component normalizes to empty.",
}


def _staging(destination: Path) -> Path:
    """The fixed staging directory a restore of ``destination`` resumes from."""
    return destination.parent / f".{PID}.restore"


def _store_files(root: Path) -> dict[str, bytes]:
    """Scaffold a store and return its files, keyed by store-relative path."""
    scaffold_project_store("nauro", root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class _Cloud:
    """A remote record behind the restore's three network calls, recording each."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.presigned: list[str] = []
        self.fetched: list[str] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(recovery, "fetch_manifest", self.manifest)
        monkeypatch.setattr(recovery, "request_presigned_urls", self.presign)
        monkeypatch.setattr(recovery, "fetch_via_presigned_url", self.fetch)

    def manifest(self, _project_id: str, **_kwargs) -> list[dict]:
        return [
            {
                "path": path,
                "etag": f'"{hashlib.md5(content).hexdigest()}"',
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(self.files.items())
        ]

    def presign(self, _project_id: str, operations: list[dict[str, str]], **_kwargs) -> list[dict]:
        self.presigned.extend(operation["path"] for operation in operations)
        expires = datetime.now(timezone.utc) + timedelta(seconds=900)
        return [
            {
                "verb": "GET",
                "path": operation["path"],
                "url": f"memory://{operation['path']}",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for operation in operations
        ]

    def fetch(self, url: str, **_kwargs) -> FetchedObject:
        path = url.removeprefix("memory://")
        self.fetched.append(path)
        body = self.files[path]
        return FetchedObject(body, f'"{hashlib.md5(body).hexdigest()}"')


def _remote(tmp_path: Path, monkeypatch, extra: dict[str, bytes]):
    """A scaffolded remote record plus ``extra`` rows, ready to restore."""
    files = _store_files(tmp_path / "source")
    cloud = _Cloud({**files, **extra})
    cloud.install(monkeypatch)
    return files, cloud, tmp_path / "projects" / PID, _RecordingReporter()


def _installed(destination: Path) -> dict[str, bytes]:
    return {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }


def _read(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _is_reserved(value) -> bool:
    for part in str(value).replace("\\", "/").split("/"):
        folded = part.split(":", 1)[0].strip(" ").rstrip(" .").casefold()
        if folded in {_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME}:
            return True
    return False


def _forbid(monkeypatch, matches, *, inspect: bool = False) -> None:
    """Fail the test when a restore reads, writes, or inspects a path ``matches`` accepts."""
    real_read = Path.read_bytes
    real_write = recovery.atomic_write_bytes
    real_lstat = merge_module.os.lstat

    def check(value) -> None:
        if matches(Path(value)):
            pytest.fail(f"reached {value!r}")

    def read_bytes(self):
        check(self)
        return real_read(self)

    def write(path, content):
        check(path)
        return real_write(path, content)

    def guarded_lstat(path, *args, **kwargs):
        check(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(recovery, "atomic_write_bytes", write)
    if inspect:
        monkeypatch.setattr(merge_module.os, "lstat", guarded_lstat)


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


def _outside_with_victim(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    return outside


def _bare_store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir()
    (store / PROJECT_MD).write_bytes(b"# project\n")
    return store


def _forbid_read(monkeypatch, *targets: Path) -> None:
    """Fail the test if the corpus reads one of ``targets``."""
    real_read = corpus_module.read_text_lenient
    resolved = {os.path.realpath(target) for target in targets}

    def read_text_lenient(path: Path) -> str:
        if os.path.realpath(path) in resolved:
            pytest.fail(f"read {path}")
        return real_read(path)

    monkeypatch.setattr(corpus_module, "read_text_lenient", read_text_lenient)


def _forbid_scandir(monkeypatch, target: Path) -> None:
    """Fail the test if the corpus lists ``target``."""
    real_scandir = corpus_module.os.scandir

    def scandir(path):
        if os.path.realpath(path) == os.path.realpath(target):
            pytest.fail(f"listed {path}")
        return real_scandir(path)

    monkeypatch.setattr(corpus_module.os, "scandir", scandir)


def test_reserved_manifest_rows_are_never_presigned_or_installed(tmp_path, monkeypatch):
    files, cloud, destination, reporter = _remote(
        tmp_path, monkeypatch, {row: b"control\n" for row in RESERVED_ROWS}
    )

    restore_cloud_store(PID, destination, reporter)

    installed = _installed(destination)
    assert installed.pop(SYNC_STATE_FILE)
    assert installed == files
    assert not [name for name in entry_names(destination) if _is_reserved(name)]
    assert sorted(cloud.presigned) == sorted(files)
    assert sorted(cloud.fetched) == sorted(files)
    state = json.loads((destination / SYNC_STATE_FILE).read_text(encoding="utf-8"))
    assert set(state["files"]) == set(files)
    assert reporter.warns == []
    assert f"Restored {len(files)} files." in reporter.infos
    assert not any(message.startswith("Skipped") for message in reporter.infos)


def test_unsafe_manifest_rows_are_named_once_and_counted(tmp_path, monkeypatch):
    files, cloud, destination, reporter = _remote(
        tmp_path, monkeypatch, {row: b"payload\n" for row in UNSAFE_ROWS}
    )

    restore_cloud_store(PID, destination, reporter)

    assert reporter.warns == [
        f"skipping manifest entry {_escape_path_for_display(row)}: {reason}"
        for row, reason in sorted(UNSAFE_ROWS.items())
    ]
    assert sorted(cloud.presigned) == sorted(files)
    assert f"Restored {len(files)} files." in reporter.infos
    assert "Skipped 2 manifest entries this restore cannot install." in reporter.infos


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "junction"])
@pytest.mark.parametrize("name", [_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME])
def test_control_state_in_staging_is_removed_without_following(tmp_path, monkeypatch, name, kind):
    if kind in LINK_KINDS:
        _require_link_kind(kind)
    _files, _cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})
    outside = _outside_with_victim(tmp_path)
    control = _staging(destination) / name
    control.parent.mkdir(parents=True)
    if kind == "file":
        control.write_bytes(b"control\n")
    elif kind == "directory":
        control.mkdir()
        (control / "authority.json").write_bytes(b"{}\n")
    else:
        _link(control, outside, kind)

    restore_cloud_store(PID, destination, reporter)

    assert not [entry for entry in entry_names(destination) if _is_reserved(entry)]
    assert reporter.warns == [CONTROL_DISCARDED]
    assert entry_names(outside) == {"victim.md"}


def test_control_state_that_cannot_be_removed_stops_the_restore(tmp_path, monkeypatch):
    _files, cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})
    control = _staging(destination) / _REPLICA_CONTROL_LOCK_NAME
    control.parent.mkdir(parents=True)
    control.write_bytes(b"control\n")
    monkeypatch.setattr(recovery, "_remove_control_entry", lambda _control: None)

    with pytest.raises(RecoveryError) as raised:
        restore_cloud_store(PID, destination, reporter)

    assert str(raised.value) == (
        "The restore staging area held local control state that could not be removed."
    )
    assert cloud.presigned == []
    assert not destination.exists()


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_linked_staging_path_is_refused(tmp_path, monkeypatch, kind):
    _require_link_kind(kind)
    _files, cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})
    outside = _outside_with_victim(tmp_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _link(_staging(destination), outside, kind)

    with pytest.raises(RecoveryError) as raised:
        restore_cloud_store(PID, destination, reporter)

    assert str(raised.value) == STAGING_UNAVAILABLE
    assert cloud.presigned == []
    assert entry_names(outside) == {"victim.md"}


def test_an_unrecorded_staged_file_is_unlinked(tmp_path, monkeypatch):
    files, _cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})
    staging = _staging(destination)
    (staging / "context").mkdir(parents=True)
    (staging / "context" / "stray.md").write_bytes(b"stray\n")

    restore_cloud_store(PID, destination, reporter)

    installed = _installed(destination)
    assert installed.pop(SYNC_STATE_FILE)
    assert installed == files
    assert "context" not in entry_names(destination)


@POSIX_ONLY
def test_a_directory_link_in_staging_deletes_nothing_outside_it(tmp_path, monkeypatch):
    _files, _cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    staging = _staging(destination)
    staging.mkdir(parents=True)
    (staging / "escape").symlink_to(outside, target_is_directory=True)

    restore_cloud_store(PID, destination, reporter)

    assert (outside / "victim.md").read_bytes() == b"victim\n"
    assert entry_names(outside) == {"victim.md"}
    assert "escape" not in entry_names(destination)


@POSIX_ONLY
def test_a_staged_file_link_is_discarded_and_downloaded_again(tmp_path, monkeypatch):
    _files, _cloud, destination, reporter = _remote(tmp_path, monkeypatch, {NOTE: b"note\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"secret\n")
    staging = _staging(destination)
    (staging / NOTE).parent.mkdir(parents=True)
    (staging / NOTE).symlink_to(secret)
    _forbid(monkeypatch, lambda path: os.path.realpath(path) == os.path.realpath(secret))

    restore_cloud_store(PID, destination, reporter)

    assert _read(destination / NOTE) == b"note\n"
    assert not (destination / NOTE).is_symlink()
    assert _read(secret) == b"secret\n"


def test_a_restore_reaches_no_reserved_path_and_no_linked_target(tmp_path, monkeypatch):
    files, _cloud, destination, reporter = _remote(
        tmp_path, monkeypatch, {row: b"control\n" for row in RESERVED_ROWS}
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    staging = _staging(destination)
    staging.mkdir(parents=True)
    if sys.platform != "win32":
        (staging / "escape").symlink_to(outside, target_is_directory=True)
    # The two fixed control names at the staging root are the one probe the
    # restore is entitled to make, and it makes it without following them.
    probes = {
        str(staging / _REPLICA_CONTROL_ROOT_NAME),
        str(staging / _REPLICA_CONTROL_LOCK_NAME),
    }
    _forbid(
        monkeypatch,
        lambda path: (
            str(path) not in probes
            and (_is_reserved(path) or path == outside or outside in path.parents)
        ),
        inspect=True,
    )

    restore_cloud_store(PID, destination, reporter)

    assert _read(outside / "victim.md") == b"victim\n"
    assert entry_names(destination) == {Path(name).parts[0] for name in files} | {SYNC_STATE_FILE}


def test_a_restore_never_enumerates_with_rglob(tmp_path, monkeypatch):
    files, _cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})

    def rglob(self, _pattern):
        pytest.fail(f"rglob {self}")

    monkeypatch.setattr(Path, "rglob", rglob)

    restore_cloud_store(PID, destination, reporter)

    assert (destination / PROJECT_MD).read_bytes() == files[PROJECT_MD]


def test_an_unusable_staging_root_is_refused_before_presign(tmp_path, monkeypatch):
    _files, cloud, destination, reporter = _remote(tmp_path, monkeypatch, {})

    def unavailable(_path):
        raise _StoreRootPreparationError()

    monkeypatch.setattr(recovery, "_prepare_store_root", unavailable)

    with pytest.raises(RecoveryError) as raised:
        restore_cloud_store(PID, destination, reporter)

    assert str(raised.value) == STAGING_UNAVAILABLE
    assert raised.value.__cause__ is None
    assert cloud.presigned == []


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_a_linked_destination_is_refused_before_any_network_call(tmp_path, monkeypatch, kind):
    _require_link_kind(kind)

    def explode(_project_id, **_kwargs):
        raise AssertionError("network must not run")

    monkeypatch.setattr(recovery, "fetch_manifest", explode)
    destination = get_store_path_v2(PID)
    target = destination.parent / "victim"
    target.mkdir(parents=True)
    (target / "keep.md").write_bytes(b"keep\n")
    _link(destination, target, kind)

    with pytest.raises(RecoveryError) as raised:
        restore_cloud_store(PID, destination)

    assert str(raised.value) == (
        f"Refusing to restore onto a link at the destination: {destination}."
    )
    assert (target / "keep.md").read_bytes() == b"keep\n"
    assert entry_names(target) == {"keep.md"}


def test_reconnect_reports_an_unusable_staging_root_and_exits_one(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(
        repo,
        {"mode": "cloud", "id": PID, "name": "nauro", "server_url": "https://example.test"},
    )
    monkeypatch.chdir(repo)
    _Cloud(_store_files(tmp_path / "source")).install(monkeypatch)

    def unavailable(_path):
        raise _StoreRootPreparationError()

    monkeypatch.setattr(recovery, "_prepare_store_root", unavailable)
    with patch("nauro.cli.commands.reconnect.require_cloud_membership", return_value="nauro"):
        result = CliRunner().invoke(app, ["reconnect"], input="restore\n")

    assert result.exit_code == 1
    assert f"Error: {STAGING_UNAVAILABLE}" in result.output


def test_a_real_decisions_directory_lists_its_regular_files(tmp_path):
    store = _bare_store(tmp_path)
    (store / DECISIONS_DIR).mkdir()
    (store / DECISIONS_DIR / "003-x.md").write_bytes(decision_bytes(3, "X"))
    (store / DECISIONS_DIR / "notes.txt").write_bytes(b"skip\n")

    corpus = DecisionCorpus.scan(store)

    assert {entry.name for entry in corpus.files} == {"003-x.md"}
    assert corpus.irregular == ()
    assert corpus.claimed_numbers() == frozenset({3})


@POSIX_ONLY
@pytest.mark.parametrize(
    ("target", "warnings"),
    [
        # A reserved target is classified before anything is inspected, so it
        # stays silent; the other two are refused on their own metadata.
        (f"{_REPLICA_CONTROL_ROOT_NAME}/{DECISIONS_DIR}", []),
        ("context", [UNUSABLE_DECISIONS]),
        ("../outside", [UNUSABLE_DECISIONS]),
    ],
)
def test_a_linked_decisions_directory_lists_nothing(
    tmp_path, monkeypatch, caplog, target, warnings
):
    store = _bare_store(tmp_path)
    listed = tmp_path / "outside" if target.startswith("..") else store / target
    listed.mkdir(parents=True)
    (listed / "003-x.md").write_bytes(decision_bytes(3, "X"))
    (store / DECISIONS_DIR).symlink_to(target, target_is_directory=True)
    _forbid_scandir(monkeypatch, listed)

    with caplog.at_level(logging.WARNING, logger="nauro.sync"):
        corpus = DecisionCorpus.scan(store)

    assert corpus.files == ()
    assert corpus.irregular == ()
    assert [record.getMessage() for record in caplog.records] == warnings


@POSIX_ONLY
def test_an_unsafe_decision_name_is_listed_but_never_read(tmp_path):
    store = _bare_store(tmp_path)
    (store / DECISIONS_DIR).mkdir()
    (store / DECISIONS_DIR / "..\\x.md").write_bytes(decision_bytes(3, "X"))

    corpus = DecisionCorpus.scan(store)

    assert corpus.irregular == (
        IrregularEntry(name="..\\x.md", number=None, reason=SkipReason.unsafe_name),
    )
    assert corpus.files == ()
    assert not corpus.has_name("..\\x.md")


@POSIX_ONLY
def test_a_linked_questions_file_is_unreadable_and_never_read(tmp_path, monkeypatch):
    store = _bare_store(tmp_path)
    (store / DECISIONS_DIR).mkdir()
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    target = control / "x"
    target.write_bytes(b"- [Q1] question\n")
    (store / OPEN_QUESTIONS_MD).symlink_to(Path(_REPLICA_CONTROL_ROOT_NAME) / "x")
    _forbid_read(monkeypatch, target, store / OPEN_QUESTIONS_MD)

    index = DecisionCorpus.scan(store).references()

    assert index.questions_readable is False
    assert index.verifiable is False
    assert index.question_refs == frozenset()


@POSIX_ONLY
def test_a_pull_reports_an_unsafe_decision_name_without_reading_it(tmp_path, monkeypatch):
    store = _scaffolded_cloud_project("unsafename", tmp_path, project_id=CLOUD_PID)
    _seed_token()
    unsafe = store / DECISIONS_DIR / "..\\x.md"
    unsafe.write_bytes(decision_bytes(3, "X"))
    _forbid_read(monkeypatch, unsafe)

    report, reporter = pull_report(store, [("notes.md", b"note\n")])

    assert report.merged == 1
    assert [warning for warning in reporter.warns if "..\\x.md" in warning] == [
        "decisions/..\\x.md: its name is not a safe path - it reads as a parent, a drive, "
        "or an empty component. Nauro never reads or changes it, and holds back any remote "
        "decision claiming its number. Rename it to a plain '.md' filename."
    ]


@POSIX_ONLY
def test_a_pull_over_a_linked_decisions_directory_writes_nothing(tmp_path, monkeypatch):
    store = _scaffolded_cloud_project("restoreadmission", tmp_path, project_id=CLOUD_PID)
    _seed_token()
    shutil.rmtree(store / DECISIONS_DIR)
    control = store / _REPLICA_CONTROL_ROOT_NAME
    control.mkdir()
    (control / "003-x.md").write_bytes(decision_bytes(3, "X"))
    (store / DECISIONS_DIR).symlink_to(_REPLICA_CONTROL_ROOT_NAME, target_is_directory=True)
    _forbid_read(monkeypatch, control / "003-x.md")
    real_replace = os.replace

    def replace(source, target):
        landing = Path(os.path.realpath(target))
        if landing == control or control in landing.parents:
            pytest.fail(f"renamed onto {target}")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)

    report, _reporter = pull_report(
        store, [(f"{DECISIONS_DIR}/003-x.md", decision_bytes(3, "Remote"))]
    )

    assert report.merged == 0
    # The pull's decision lock still lands here through the link; it is the one
    # known stray, so the filter must not absorb a second.
    assert entry_names(control) == {"003-x.md", ".lock"}
    assert (control / "003-x.md").read_bytes() == decision_bytes(3, "X")
