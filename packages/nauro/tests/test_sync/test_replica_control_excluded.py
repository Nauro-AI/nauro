from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from nauro.store.replica_control import (
    _REPLICA_CONTROL_LOCK_NAME,
    _REPLICA_CONTROL_ROOT_NAME,
)
from nauro.sync import _windows_long_names as long_names
from nauro.sync import merge
from nauro.sync._path_diagnostics import (
    _escape_path_for_display,
    _MissingPathPolicy,
    _NativeKind,
    _PathClass,
    _StoreRootPreparationError,
    _unsafe_reason_text,
    _UnsafeReason,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".replica", _PathClass.RESERVED_CONTROL),
        (".replica/authority.json", _PathClass.RESERVED_CONTROL),
        (".replica\\authority.json", _PathClass.RESERVED_CONTROL),
        (" .RePlIcA. :stream/authority.json", _PathClass.RESERVED_CONTROL),
        (".replica-control.lock", _PathClass.RESERVED_CONTROL),
        (".replica-control.lock\\child", _PathClass.RESERVED_CONTROL),
        (".replica .", _PathClass.RESERVED_CONTROL),
        (".replica. .", _PathClass.RESERVED_CONTROL),
        (".replica-control.lock .", _PathClass.RESERVED_CONTROL),
        (".replica-control.lock. . ", _PathClass.RESERVED_CONTROL),
        (".replica ./authority.json", _PathClass.RESERVED_CONTROL),
        (".replica2/file", _PathClass.ORDINARY),
        (".replica-control.lock.bak", _PathClass.ORDINARY),
        ("context/.replica/file", _PathClass.ORDINARY),
        ("context/.replica-control.lock", _PathClass.ORDINARY),
        ("context/replica-notes.md", _PathClass.ORDINARY),
    ],
)
def test_semantic_control_aliases(raw: str, expected: _PathClass) -> None:
    assert merge._classify_sync_path(raw).path_class is expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", _UnsafeReason.EMPTY_PATH),
        ("/absolute", _UnsafeReason.RAW_ABSOLUTE),
        ("\\rooted", _UnsafeReason.RAW_ROOTED),
        ("C:relative", _UnsafeReason.RAW_DRIVE),
        ("C:\\absolute", _UnsafeReason.RAW_DRIVE),
        ("\\\\server\\share\\file", _UnsafeReason.RAW_UNC),
        ("\\\\?\\C:\\file", _UnsafeReason.RAW_DEVICE),
        ("//?/C:/file", _UnsafeReason.RAW_DEVICE),
        ("\\\\?/C:/file", _UnsafeReason.RAW_DEVICE),
        ("//./C:/file", _UnsafeReason.RAW_DEVICE),
        ("\\\\./C:/file", _UnsafeReason.RAW_DEVICE),
        ("../file", _UnsafeReason.RAW_PARENT),
        ("context/../file", _UnsafeReason.RAW_PARENT),
        ("context/ .. ", _UnsafeReason.FOLDED_PARENT),
        ("context/ . ", _UnsafeReason.FOLDED_DOT),
        ("context/. .", _UnsafeReason.FOLDED_EMPTY),
        ("context/ :stream", _UnsafeReason.FOLDED_EMPTY),
    ],
)
def test_hostile_sync_paths_are_unsafe(raw: str, reason: _UnsafeReason) -> None:
    admission = merge._classify_sync_path(raw)
    assert (admission.path_class, admission.reason) == (_PathClass.UNSAFE, reason)
    assert admission.raw_identity == raw


def test_later_unsafe_component_wins_over_reserved_prefix() -> None:
    admission = merge._classify_sync_path(".replica/context/ :stream")
    assert (admission.path_class, admission.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.FOLDED_EMPTY,
    )


def test_shared_control_names() -> None:
    assert (_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME) == (
        ".replica",
        ".replica-control.lock",
    )


def test_reason_text_is_closed_and_fixed() -> None:
    expected = {
        _UnsafeReason.RAW_ABSOLUTE: "The path is absolute.",
        _UnsafeReason.RAW_ROOTED: "The path is rooted.",
        _UnsafeReason.RAW_DRIVE: "The path uses drive syntax.",
        _UnsafeReason.RAW_UNC: "The path uses UNC syntax.",
        _UnsafeReason.RAW_DEVICE: "The path uses device syntax.",
        _UnsafeReason.RAW_PARENT: "The path contains a parent component.",
        _UnsafeReason.EMPTY_PATH: "The path has no usable component.",
        _UnsafeReason.FOLDED_EMPTY: "A path component normalizes to empty.",
        _UnsafeReason.FOLDED_DOT: "A path component normalizes to dot.",
        _UnsafeReason.FOLDED_PARENT: "A path component normalizes to parent.",
        _UnsafeReason.OUTSIDE_STORE: "The path resolves outside the Store root.",
        _UnsafeReason.OBSERVATION_LOST: "The path changed after it was observed.",
        _UnsafeReason.WINDOWS_NAME_LOOKUP_FAILED: "The Windows long-name lookup failed.",
        _UnsafeReason.METADATA_UNAVAILABLE: "Path metadata is unavailable.",
        _UnsafeReason.LINK_TARGET_UNREADABLE: "The link target is unavailable.",
        _UnsafeReason.UNSUPPORTED_REPARSE: "The path uses an unsupported reparse point.",
        _UnsafeReason.LINK_LOOP: "The path contains a link loop.",
        _UnsafeReason.LINK_HOP_LIMIT: "The path exceeds the link hop limit.",
        _UnsafeReason.NON_DIRECTORY_PARENT: ("A non-directory path has a remaining component."),
    }
    assert {reason: _unsafe_reason_text(reason) for reason in _UnsafeReason} == expected


def test_display_escape_is_one_line_ascii() -> None:
    raw = "space name\n\r\x1b'\"\\café\x00\x7f"
    escaped = _escape_path_for_display(raw)
    assert escaped == ("space\\x20name\\n\\r\\x1B\\x27\\x22\\\\caf\\xC3\\xA9\\x00\\x7F")
    assert escaped.isascii()
    assert not set("\n\r\x1b") & set(escaped)


def test_display_escape_round_trips_surrogateescaped_byte() -> None:
    assert _escape_path_for_display("name-\udcff") == "name-\\xFF"


def test_prepare_store_root_retains_configured_and_canonical_paths(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.chdir(tmp_path)
    prepared = merge._prepare_store_root(Path("store"))
    assert prepared.configured_root == Path("store")
    assert not prepared.configured_root.is_absolute()
    assert prepared.canonical_root == store.resolve()
    assert prepared.canonical_root.is_absolute()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_prepare_store_root_has_fixed_suppressed_failure(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    if kind == "file":
        root.write_text("not a directory")
    with pytest.raises(_StoreRootPreparationError) as raised:
        merge._prepare_store_root(root)
    assert str(raised.value) == "The Store root is unavailable."
    assert raised.value.__cause__ is None
    assert str(root) not in str(raised.value)


def test_admission_has_exactly_one_enumerating_caller() -> None:
    src_root = Path(merge.__file__).parents[2]
    allowed = {
        "_walk_store_files": {"sync/merge.py", "sync/push.py", "sync/pull.py"},
        "_prepare_store_root": {"sync/merge.py", "sync/push.py", "sync/pull.py"},
        "_admit_native_path": {"sync/merge.py", "sync/pull.py"},
        "_classify_sync_path": {"sync/merge.py", "sync/pull.py"},
    }
    for source in src_root.rglob("*.py"):
        relative = source.relative_to(src_root / "nauro").as_posix()
        text = source.read_text(encoding="utf-8")
        for name, owners in allowed.items():
            assert name not in text or relative in owners, (name, relative)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX literal filename semantics")
def test_posix_literal_control_alias_stops_before_metadata(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    alias = store / ".replica\\child"
    alias.write_text("control")
    prepared = merge._prepare_store_root(store)
    real_lstat = merge.os.lstat

    def guarded_lstat(path: os.PathLike[str] | str):
        if Path(path) == alias:
            pytest.fail("reserved alias metadata")
        return real_lstat(path)

    monkeypatch.setattr(merge.os, "lstat", guarded_lstat)
    result = merge._admit_native_path(prepared, alias.name, missing=_MissingPathPolicy.OBSERVED)
    assert result.path_class is _PathClass.RESERVED_CONTROL


@pytest.mark.parametrize(
    ("raw", "path_class", "reason"),
    [
        ("./project.md", _PathClass.ORDINARY, None),
        ("context//notes.md/", _PathClass.ORDINARY, None),
        ("context\\\\notes.md\\", _PathClass.ORDINARY, None),
        (" .replica /pointer.json", _PathClass.RESERVED_CONTROL, None),
        (".REPLICA.../pointer.json", _PathClass.RESERVED_CONTROL, None),
        (".replica:stream/pointer.json", _PathClass.RESERVED_CONTROL, None),
        (".replica-control.lock :ads", _PathClass.RESERVED_CONTROL, None),
        ("context/.", _PathClass.ORDINARY, None),
        ("context/...", _PathClass.UNSAFE, _UnsafeReason.FOLDED_EMPTY),
        ("context/  ", _PathClass.UNSAFE, _UnsafeReason.FOLDED_EMPTY),
        ("context/.. ", _PathClass.UNSAFE, _UnsafeReason.FOLDED_PARENT),
        ("context/. ", _PathClass.UNSAFE, _UnsafeReason.FOLDED_DOT),
    ],
)
def test_semantic_normalization_table(
    raw: str, path_class: _PathClass, reason: _UnsafeReason | None
) -> None:
    admission = merge._classify_sync_path(raw)
    assert (admission.path_class, admission.reason) == (path_class, reason)


def test_component_normalizer_expands_both_separators_without_changing_identity() -> None:
    exact = ("context", "notes\\draft:stream")
    view, reason = merge._normalize_component_path(exact, raw_identity="raw\\identity")
    assert reason is None
    assert view.raw_identity == "raw\\identity"
    assert view.exact_components == exact
    assert view.semantic_components == ("context", "notes", "draft")


def test_admission_results_are_immutable_and_validate_invariants() -> None:
    result = merge._classify_sync_path("project.md")
    with pytest.raises(FrozenInstanceError):
        result.raw_identity = "changed"
    with pytest.raises(ValueError, match="unsafe path requires"):
        merge._PathAdmission(_PathClass.UNSAFE, "raw")
    with pytest.raises(ValueError, match="Only an unsafe"):
        merge._PathAdmission(_PathClass.ORDINARY, "raw", reason=_UnsafeReason.METADATA_UNAVAILABLE)


def test_optional_fixed_leaf_and_observed_missing(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    optional = merge._admit_native_path(
        prepared, "new.json", missing=_MissingPathPolicy.OPTIONAL_FIXED_LEAF
    )
    observed = merge._admit_native_path(prepared, "new.json", missing=_MissingPathPolicy.OBSERVED)
    assert (
        optional.path_class,
        optional.exists,
        optional.missing_policy,
        optional.native_kind,
    ) == (
        _PathClass.ORDINARY,
        False,
        _MissingPathPolicy.OPTIONAL_FIXED_LEAF,
        None,
    )
    assert (observed.path_class, observed.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.OBSERVATION_LOST,
    )


def test_optional_fixed_leaf_rejects_missing_intermediate(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    result = merge._admit_native_path(
        merge._prepare_store_root(store),
        os.path.join("missing", "leaf.json"),
        missing=_MissingPathPolicy.OPTIONAL_FIXED_LEAF,
    )
    assert (result.path_class, result.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.OBSERVATION_LOST,
    )


def test_create_destination_stops_probing_after_first_absence(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    real_adapter = merge._existing_long_component
    real_lstat = merge.os.lstat
    operations: list[tuple[str, str]] = []

    def probing_adapter(parent: Path, component: str) -> str | None:
        operations.append(("adapter", component))
        return real_adapter(parent, component)

    def probing_lstat(path: os.PathLike[str] | str):
        operations.append(("lstat", Path(path).name))
        return real_lstat(path)

    monkeypatch.setattr(merge, "_existing_long_component", probing_adapter)
    monkeypatch.setattr(merge.os, "lstat", probing_lstat)
    result = merge._admit_native_path(
        prepared,
        os.path.join("missing", "child", "new.md"),
        missing=_MissingPathPolicy.CREATE_DESTINATION,
    )
    assert (result.path_class, result.exists, result.missing_policy) == (
        _PathClass.ORDINARY,
        False,
        _MissingPathPolicy.CREATE_DESTINATION,
    )
    if sys.platform == "win32":
        expected = [("adapter", "missing")]
    else:
        expected = [("adapter", "missing"), ("lstat", "missing")]
    assert operations == expected
    assert not {name for _, name in operations} & {"child", "new.md"}


@pytest.mark.parametrize(
    ("raw", "path_class", "reason"),
    [
        (" .replica. /child/new.md", _PathClass.RESERVED_CONTROL, None),
        ("missing/ :stream/new.md", _PathClass.UNSAFE, _UnsafeReason.FOLDED_EMPTY),
        ("missing/ .. /new.md", _PathClass.UNSAFE, _UnsafeReason.FOLDED_PARENT),
    ],
)
def test_lexical_precheck_rejects_reserved_or_unsafe_suffix_before_access(
    tmp_path: Path,
    monkeypatch,
    raw: str,
    path_class: _PathClass,
    reason: _UnsafeReason | None,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    monkeypatch.setattr(merge.os, "lstat", lambda *_: pytest.fail("metadata access"))
    result = merge._admit_native_path(prepared, raw, missing=_MissingPathPolicy.CREATE_DESTINATION)
    assert (result.path_class, result.reason) == (path_class, reason)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("missing/..", _UnsafeReason.FOLDED_PARENT),
        ("missing/ :stream/new.md", _UnsafeReason.FOLDED_EMPTY),
    ],
)
def test_create_destination_rejects_link_inserted_suffix_after_absence(
    tmp_path: Path, monkeypatch, target: str, reason: _UnsafeReason
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "link").symlink_to(target)
    prepared = merge._prepare_store_root(store)
    real_adapter = merge._existing_long_component
    real_lstat = merge.os.lstat
    absent = store / "missing"

    def guarded_adapter(parent: Path, component: str) -> str | None:
        if parent == absent or absent in parent.parents:
            pytest.fail("adapter under the absent component")
        if component in {" :stream", "new.md", ".."}:
            pytest.fail("adapter on the inserted suffix")
        assert component in {"link", "missing"}
        return real_adapter(parent, component)

    def guarded_lstat(path: os.PathLike[str] | str):
        if absent in Path(path).parents:
            pytest.fail("metadata under the absent component")
        return real_lstat(path)

    monkeypatch.setattr(merge, "_existing_long_component", guarded_adapter)
    monkeypatch.setattr(merge.os, "lstat", guarded_lstat)
    result = merge._admit_native_path(
        prepared, "link", missing=_MissingPathPolicy.CREATE_DESTINATION
    )
    assert (result.path_class, result.reason) == (_PathClass.UNSAFE, reason)


def test_create_destination_skips_raw_dot_after_first_absence(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    result = merge._admit_native_path(
        merge._prepare_store_root(store),
        "missing/./new.md",
        missing=_MissingPathPolicy.CREATE_DESTINATION,
    )
    assert (result.path_class, result.exists) == (_PathClass.ORDINARY, False)


@pytest.mark.parametrize(
    "missing", [_MissingPathPolicy.OBSERVED, _MissingPathPolicy.CREATE_DESTINATION]
)
def test_embedded_nul_is_unsafe_without_access(
    tmp_path: Path, monkeypatch, missing: _MissingPathPolicy
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    monkeypatch.setattr(merge.os, "lstat", lambda *_: pytest.fail("metadata access"))
    monkeypatch.setattr(merge, "_existing_long_component", lambda *_: pytest.fail("adapter access"))
    result = merge._admit_native_path(prepared, "nul\x00name", missing=missing)
    assert (result.path_class, result.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.METADATA_UNAVAILABLE,
    )


def test_returned_long_name_is_classified_before_metadata(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    monkeypatch.setattr(
        merge,
        "_existing_long_component",
        lambda _parent, component: (
            _REPLICA_CONTROL_ROOT_NAME if component == "REPLIC~1" else component
        ),
    )
    monkeypatch.setattr(merge.os, "lstat", lambda *_: pytest.fail("metadata access"))
    result = merge._admit_native_path(
        prepared, "REPLIC~1/pointer.json", missing=_MissingPathPolicy.OBSERVED
    )
    assert result.path_class is _PathClass.RESERVED_CONTROL


@pytest.mark.parametrize("raw", [".replica/pointer.json", "context/ :stream"])
def test_adapter_not_called_for_reserved_or_unsafe_component(
    tmp_path: Path, monkeypatch, raw: str
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    prepared = merge._prepare_store_root(store)
    monkeypatch.setattr(merge, "_existing_long_component", lambda *_: pytest.fail("adapter access"))
    monkeypatch.setattr(merge.os, "lstat", lambda *_: pytest.fail("metadata access"))
    result = merge._admit_native_path(prepared, raw, missing=_MissingPathPolicy.OBSERVED)
    assert result.path_class is not _PathClass.ORDINARY


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_adapter_not_called_for_target_inserted_control_component(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "alias").symlink_to(f"{_REPLICA_CONTROL_ROOT_NAME}/pointer.json")
    prepared = merge._prepare_store_root(store)

    def guarded_adapter(_parent: Path, component: str) -> str:
        if component == _REPLICA_CONTROL_ROOT_NAME:
            pytest.fail("adapter access on control component")
        return component

    monkeypatch.setattr(merge, "_existing_long_component", guarded_adapter)
    result = merge._admit_native_path(prepared, "alias", missing=_MissingPathPolicy.OBSERVED)
    assert result.path_class is _PathClass.RESERVED_CONTROL


def test_create_destination_rejects_existing_non_directory_parent(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "file").write_text("content")
    result = merge._admit_native_path(
        merge._prepare_store_root(store),
        os.path.join("file", "child"),
        missing=_MissingPathPolicy.CREATE_DESTINATION,
    )
    assert (result.path_class, result.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.NON_DIRECTORY_PARENT,
    )


def test_native_kinds_and_trailing_separator(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "directory").mkdir()
    (store / "file").write_text("content")
    prepared = merge._prepare_store_root(store)
    directory = merge._admit_native_path(prepared, "directory", missing=_MissingPathPolicy.OBSERVED)
    regular = merge._admit_native_path(prepared, "file", missing=_MissingPathPolicy.OBSERVED)
    trailing = merge._admit_native_path(
        prepared, f"file{os.sep}", missing=_MissingPathPolicy.OBSERVED
    )
    assert directory.native_kind is _NativeKind.DIRECTORY
    assert regular.native_kind is _NativeKind.REGULAR_FILE
    assert trailing.reason is _UnsafeReason.NON_DIRECTORY_PARENT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_link_target_into_each_control_root_stops_before_target_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    targets = [store / _REPLICA_CONTROL_ROOT_NAME, store / _REPLICA_CONTROL_LOCK_NAME]
    targets[0].mkdir()
    targets[1].write_text("lock")
    for index, target in enumerate(targets):
        (store / f"alias-{index}").symlink_to(target, target_is_directory=target.is_dir())
    prepared = merge._prepare_store_root(store)
    real_lstat = merge.os.lstat

    def guarded_lstat(path: os.PathLike[str] | str):
        if Path(path) in targets:
            pytest.fail("control target metadata")
        return real_lstat(path)

    monkeypatch.setattr(merge.os, "lstat", guarded_lstat)
    for index in range(2):
        result = merge._admit_native_path(
            prepared, f"alias-{index}", missing=_MissingPathPolicy.OBSERVED
        )
        assert result.path_class is _PathClass.RESERVED_CONTROL


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_absolute_outside_link_stops_before_outside_metadata(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    outside = tmp_path / "store-evil"
    store.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (store / "alias").symlink_to(outside / "secret")
    prepared = merge._prepare_store_root(store)
    real_lstat = merge.os.lstat

    def guarded_lstat(path: os.PathLike[str] | str):
        if outside in Path(path).parents or Path(path) == outside:
            pytest.fail("outside metadata")
        return real_lstat(path)

    monkeypatch.setattr(merge.os, "lstat", guarded_lstat)
    result = merge._admit_native_path(prepared, "alias", missing=_MissingPathPolicy.OBSERVED)
    assert (result.path_class, result.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.OUTSIDE_STORE,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_native_link_resolution_expands_before_parent(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "subdir").mkdir(parents=True)
    (store / "a").symlink_to(f"{_REPLICA_CONTROL_ROOT_NAME}/subdir")
    (store / "x").symlink_to("a/../secret")
    result = merge._admit_native_path(
        merge._prepare_store_root(store), "x", missing=_MissingPathPolicy.OBSERVED
    )
    assert result.path_class is _PathClass.RESERVED_CONTROL


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_native_link_parent_cannot_pop_above_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    store.mkdir()
    outside.mkdir()
    (store / "a").symlink_to("../outside/subdir")
    (store / "x").symlink_to("a/../secret")
    result = merge._admit_native_path(
        merge._prepare_store_root(store), "x", missing=_MissingPathPolicy.OBSERVED
    )
    assert result.reason is _UnsafeReason.OUTSIDE_STORE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_nested_link_to_file_rejects_remaining_components(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "file").write_text("content")
    (store / "inner").symlink_to("file")
    (store / "outer").symlink_to("inner/child")
    result = merge._admit_native_path(
        merge._prepare_store_root(store), "outer", missing=_MissingPathPolicy.OBSERVED
    )
    assert result.reason is _UnsafeReason.NON_DIRECTORY_PARENT


def test_unsupported_reparse_point_is_rejected_before_readlink(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    (store / "cloud").mkdir(parents=True)
    prepared = merge._prepare_store_root(store)
    reparse = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=0x400,
        st_reparse_tag=0x80000017,
        st_dev=0,
        st_ino=0,
    )
    monkeypatch.setattr(merge.os, "lstat", lambda *_: reparse)
    monkeypatch.setattr(
        merge.os, "readlink", lambda *_: pytest.fail("readlink on unsupported reparse")
    )
    result = merge._admit_native_path(prepared, "cloud", missing=_MissingPathPolicy.OBSERVED)
    assert (result.path_class, result.reason) == (
        _PathClass.UNSAFE,
        _UnsafeReason.UNSUPPORTED_REPARSE,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_unreadable_link_target_is_rejected(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "link").symlink_to("target")
    prepared = merge._prepare_store_root(store)

    def failing_readlink(*_: object) -> str:
        raise OSError("sentinel readlink detail")

    monkeypatch.setattr(merge.os, "readlink", failing_readlink)
    result = merge._admit_native_path(prepared, "link", missing=_MissingPathPolicy.OBSERVED)
    assert result.reason is _UnsafeReason.LINK_TARGET_UNREADABLE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_link_loop_and_hop_limit(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "loop-a").symlink_to("loop-b")
    (store / "loop-b").symlink_to("loop-a")
    prepared = merge._prepare_store_root(store)
    loop = merge._admit_native_path(prepared, "loop-a", missing=_MissingPathPolicy.OBSERVED)
    assert loop.reason is _UnsafeReason.LINK_LOOP

    (store / "target").write_text("target")
    for index in range(41, 0, -1):
        target = "target" if index == 41 else f"hop-{index + 1}"
        (store / f"hop-{index}").symlink_to(target)
    at_limit = merge._admit_native_path(prepared, "hop-2", missing=_MissingPathPolicy.OBSERVED)
    over_limit = merge._admit_native_path(prepared, "hop-1", missing=_MissingPathPolicy.OBSERVED)
    assert at_limit.path_class is _PathClass.ORDINARY
    assert over_limit.reason is _UnsafeReason.LINK_HOP_LIMIT


def _prepared_windows_root(*parts: str, anchor: str = "C:\\"):
    return merge._PreparedStoreRoot(
        configured_root=Path("configured"),
        canonical_root=Path("canonical"),
        native_anchor=anchor,
        canonical_parts=parts,
    )


@pytest.mark.parametrize(
    ("target", "parts", "reason", "absolute"),
    [
        ("C:\\Store\\child", ["child"], None, True),
        ("C:\\store\\child", None, _UnsafeReason.OUTSIDE_STORE, True),
        ("C:\\StoreEvil\\child", None, _UnsafeReason.OUTSIDE_STORE, True),
        ("D:\\Store\\child", None, _UnsafeReason.OUTSIDE_STORE, True),
        ("C:relative", None, _UnsafeReason.OUTSIDE_STORE, False),
        ("\\rooted", None, _UnsafeReason.OUTSIDE_STORE, False),
        ("\\\\?\\C:\\Store\\child", ["child"], None, True),
        ("\\??\\C:\\Store\\child", ["child"], None, True),
        ("\\\\?\\Volume{abc}\\child", None, _UnsafeReason.UNSUPPORTED_REPARSE, False),
        ("\\Device\\HarddiskVolume1\\child", None, _UnsafeReason.UNSUPPORTED_REPARSE, False),
    ],
)
def test_windows_target_anchor_and_exact_component_comparison(
    monkeypatch,
    target: str,
    parts: list[str] | None,
    reason: _UnsafeReason | None,
    absolute: bool,
) -> None:
    monkeypatch.setattr(merge.sys, "platform", "win32")
    assert merge._absolute_target_suffix(target, _prepared_windows_root("Store")) == (
        parts,
        reason,
        absolute,
    )


def test_windows_unc_target_routing(monkeypatch) -> None:
    monkeypatch.setattr(merge.sys, "platform", "win32")
    root = _prepared_windows_root("Root", anchor="\\\\server\\share\\")
    assert merge._absolute_target_suffix("\\\\SERVER\\SHARE\\Root\\file", root) == (
        ["file"],
        None,
        True,
    )
    assert merge._absolute_target_suffix("\\\\server\\other\\Root\\file", root)[1] is (
        _UnsafeReason.OUTSIDE_STORE
    )
    assert merge._absolute_target_suffix("\\\\?\\UNC\\server\\share\\Root\\file", root) == (
        ["file"],
        None,
        True,
    )


def test_long_name_adapter_is_noop_off_windows(tmp_path: Path) -> None:
    if sys.platform != "win32":
        assert long_names._existing_long_component(tmp_path, "Exact:Name") == "Exact:Name"


def test_long_name_adapter_grows_buffer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(long_names.sys, "platform", "win32")
    monkeypatch.setattr(long_names.ctypes, "set_last_error", lambda _: None, raising=False)
    calls: list[int] = []

    def fake_lookup(path: str, buffer, size: int) -> int:
        calls.append(size)
        if len(calls) == 1:
            return 400
        buffer.value = os.fspath(tmp_path / "LongName.txt")
        return len(buffer.value)

    monkeypatch.setattr(long_names, "_get_long_path_name", fake_lookup)
    assert long_names._existing_long_component(tmp_path, "LONGNA~1.TXT") == "LongName.txt"
    assert calls == [260, 401]


def test_long_name_adapter_uses_last_error_handle(monkeypatch, tmp_path: Path) -> None:
    recorded: dict[str, object] = {}

    class _FakeKernel32:
        def __init__(self, name: str, **kwargs: object) -> None:
            recorded["name"] = name
            recorded.update(kwargs)

        def __getattr__(self, _name: str):
            def lookup(_path: str, buffer, _size: int) -> int:
                buffer.value = os.fspath(tmp_path / "Long.txt")
                return len(buffer.value)

            return lookup

    monkeypatch.setattr(long_names.sys, "platform", "win32")
    monkeypatch.setattr(long_names.ctypes, "WinDLL", _FakeKernel32, raising=False)
    monkeypatch.setattr(long_names.ctypes, "set_last_error", lambda _: None, raising=False)
    assert long_names._existing_long_component(tmp_path, "LONG~1.TXT") == "Long.txt"
    assert (recorded["name"], recorded.get("use_last_error")) == ("kernel32", True)


@pytest.mark.parametrize("error", [2, 3])
def test_long_name_adapter_maps_only_confirmed_not_found(monkeypatch, tmp_path, error) -> None:
    monkeypatch.setattr(long_names.sys, "platform", "win32")
    monkeypatch.setattr(long_names.ctypes, "set_last_error", lambda _: None, raising=False)
    monkeypatch.setattr(long_names.ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(long_names, "_get_long_path_name", lambda *_: 0)
    assert long_names._existing_long_component(tmp_path, "missing") is None


def test_long_name_adapter_raises_other_errors_without_embedding_them(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(long_names.sys, "platform", "win32")
    monkeypatch.setattr(long_names.ctypes, "set_last_error", lambda _: None, raising=False)
    monkeypatch.setattr(long_names.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(long_names, "_get_long_path_name", lambda *_: 0)
    with pytest.raises(OSError):
        long_names._existing_long_component(tmp_path, "sentinel-secret")
    assert _unsafe_reason_text(_UnsafeReason.WINDOWS_NAME_LOOKUP_FAILED) == (
        "The Windows long-name lookup failed."
    )


class _EntryProxy:
    def __init__(self, entry: os.DirEntry[str], *, forbid_follow: bool = False) -> None:
        self._entry = entry
        self.name = entry.name
        self.path = entry.path
        self.calls: list[bool] = []
        self._forbid_follow = forbid_follow

    def stat(self, *, follow_symlinks: bool = True):
        self.calls.append(follow_symlinks)
        if follow_symlinks and self._forbid_follow:
            pytest.fail("following metadata before admission")
        return self._entry.stat(follow_symlinks=follow_symlinks)

    def is_file(self, *args, **kwargs):
        pytest.fail("is_file called")

    def is_dir(self, *args, **kwargs):
        pytest.fail("is_dir called")


class _Listing:
    def __init__(self, entries: list[_EntryProxy]) -> None:
        self.entries = entries

    def __enter__(self):
        return self

    def __iter__(self):
        return iter(self.entries)

    def __exit__(self, *_):
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walker_prunes_control_links_before_following_or_descent(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_LOCK_NAME).write_text("lock")
    (store / "root-link").symlink_to(store / _REPLICA_CONTROL_ROOT_NAME, True)
    (store / "lock-link").symlink_to(store / _REPLICA_CONTROL_LOCK_NAME)
    with os.scandir(store) as listing:
        proxies = [
            _EntryProxy(entry, forbid_follow=entry.name.endswith("-link")) for entry in listing
        ]
    real_scandir = merge.os.scandir

    def guarded_scandir(path: os.PathLike[str] | str):
        if Path(path) == store:
            return _Listing(proxies)
        if Path(path).name in {_REPLICA_CONTROL_ROOT_NAME, _REPLICA_CONTROL_LOCK_NAME}:
            pytest.fail("control descent")
        return real_scandir(path)

    monkeypatch.setattr(merge.os, "scandir", guarded_scandir)
    assert list(merge._walk_store_files(merge._prepare_store_root(store))) == []
    link_proxies = [entry for entry in proxies if entry.name.endswith("-link")]
    assert [entry.calls for entry in link_proxies] == [[False], [False]]


def test_walker_yields_files_and_prunes_reserved_entries(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "file.md").write_text("file")
    (store / "context").mkdir()
    (store / "context" / "note.md").write_text("note")
    (store / _REPLICA_CONTROL_ROOT_NAME).mkdir()
    (store / _REPLICA_CONTROL_ROOT_NAME / "secret").write_text("secret")
    (store / _REPLICA_CONTROL_LOCK_NAME).write_text("lock")
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    assert {entry.raw_relative_path for entry in results} == {
        "file.md",
        os.path.join("context", "note.md"),
    }
    assert all(entry.admission.native_kind is _NativeKind.REGULAR_FILE for entry in results)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX literal filename semantics")
def test_walker_prunes_literal_backslash_alias_before_metadata(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    alias = store / ".replica\\secret"
    alias.write_text("secret")
    with os.scandir(store) as listing:
        proxy = _EntryProxy(next(iter(listing)))
    monkeypatch.setattr(merge.os, "scandir", lambda *_: _Listing([proxy]))
    assert list(merge._walk_store_files(merge._prepare_store_root(store))) == []
    assert proxy.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walker_keeps_file_link_and_skips_directory_link(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "target.txt").write_text("target")
    (store / "directory").mkdir()
    (store / "directory" / "nested.txt").write_text("nested")
    (store / "file-link").symlink_to("target.txt")
    (store / "dir-link").symlink_to("directory", True)
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    names = {entry.raw_relative_path for entry in results}
    assert {"target.txt", "file-link", os.path.join("directory", "nested.txt")} == names
    assert "dir-link" not in names


@pytest.mark.skipif(sys.platform == "win32", reason="Windows normalizes the unsafe name away")
def test_walker_yields_unsafe_entry_once_and_prunes(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    unsafe = store / " .. "
    unsafe.mkdir()
    (unsafe / "hidden").write_text("hidden")
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    assert len(results) == 1
    assert results[0].raw_relative_path == " .. "
    assert results[0].admission.reason is _UnsafeReason.FOLDED_PARENT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fifo semantics")
def test_walker_skips_irregular_file(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    os.mkfifo(store / "pipe")
    (store / "file.md").write_text("file")
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    assert {entry.raw_relative_path for entry in results} == {"file.md"}


def test_walker_survives_deep_tree_without_recursion(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    depth = 1200

    def fake_scandir(path: os.PathLike[str] | str):
        level = len(Path(path).relative_to(store).parts)
        if level < depth:
            name, mode = "d", stat.S_IFDIR
        else:
            name, mode = "leaf.md", stat.S_IFREG
        entry = SimpleNamespace(
            name=name,
            path=os.fspath(Path(path) / name),
            stat=lambda follow_symlinks=True: SimpleNamespace(st_mode=mode),
        )
        return _Listing([entry])

    monkeypatch.setattr(merge.os, "scandir", fake_scandir)
    monkeypatch.setattr(merge, "_existing_long_component", lambda _parent, name: name)
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    assert [entry.raw_relative_path for entry in results] == [
        os.path.join(*(["d"] * depth), "leaf.md")
    ]
    assert results[0].admission.native_kind is _NativeKind.REGULAR_FILE


def test_walker_yields_unscannable_directory_once(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    blocked = store / "blocked"
    blocked.mkdir(parents=True)
    real_scandir = merge.os.scandir

    def guarded_scandir(path: os.PathLike[str] | str):
        if Path(path) == blocked:
            raise OSError("sentinel filesystem detail")
        return real_scandir(path)

    monkeypatch.setattr(merge.os, "scandir", guarded_scandir)
    results = list(merge._walk_store_files(merge._prepare_store_root(store)))
    assert len(results) == 1
    assert results[0].native_path == blocked
    assert results[0].admission.reason is _UnsafeReason.METADATA_UNAVAILABLE
    assert "sentinel" not in _unsafe_reason_text(results[0].admission.reason)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction coverage")
def test_real_windows_junction_into_control_is_reserved(tmp_path: Path) -> None:
    store = tmp_path / "store"
    target = store / _REPLICA_CONTROL_ROOT_NAME
    junction = store / "junction"
    target.mkdir(parents=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.fspath(junction), os.fspath(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = merge._admit_native_path(
        merge._prepare_store_root(store), "junction", missing=_MissingPathPolicy.OBSERVED
    )
    assert result.path_class is _PathClass.RESERVED_CONTROL


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 8.3 alias coverage")
def test_real_windows_short_name_is_classified_from_returned_alias(tmp_path: Path) -> None:
    import ctypes

    directory = tmp_path / "Long Foundation Directory"
    directory.mkdir()
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(  # type: ignore[attr-defined]
        os.fspath(directory), buffer, len(buffer)
    )
    if not length:
        pytest.fail("GetShortPathNameW failed")
    if Path(buffer.value).name == directory.name:
        pytest.skip("Windows did not provide a distinct 8.3 alias")
    alias = Path(buffer.value).name
    assert long_names._existing_long_component(directory.parent, alias) == directory.name
