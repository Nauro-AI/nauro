"""Kernel-level tests for ``operations.diff_since_last_session``.

Each test seeds an ``InMemoryStore`` (the kernel does not touch it for
this operation, but the locked signature still takes it) and synthesises
snapshot dicts inline. Surface-level wiring tests live in the consumer
package — snapshot discovery (``list_snapshots`` / ``load_snapshot`` /
``find_snapshot_near_date``) sits outside the locked Store protocol and
the kernel must never import it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from nauro_core.operations import (
    DiffSinceLastSessionResult,
    InMemoryStore,
    diff_since_last_session,
)


def _snapshot(version: int, timestamp: str, files: dict[str, str]) -> dict:
    return {"version": version, "timestamp": timestamp, "files": files}


def _baseline() -> dict:
    return _snapshot(
        1,
        "2026-05-01T10:00:00+00:00",
        {
            "state_current.md": "# Current State\n\n**Sprint:** alpha\n\n- Set up CI\n",
            "stack.md": "# Stack\n- Python 3.11\n",
            "open-questions.md": "# Open Questions\n- [Q1] Pick a queue?\n",
        },
    )


def _latest() -> dict:
    return _snapshot(
        2,
        "2026-05-02T10:00:00+00:00",
        {
            "state_current.md": "# Current State\n\n**Sprint:** beta\n\n- Set up CI\n",
            "stack.md": "# Stack\n- Python 3.11\n- PostgreSQL\n",
            "open-questions.md": "# Open Questions\n- [Q1] Pick a queue?\n",
            "decisions/001-adopt-postgres.md": "# Adopt Postgres\n\nReasoned.\n",
        },
    )


def test_returns_result_type() -> None:
    result = diff_since_last_session(InMemoryStore(), None, None)
    assert isinstance(result, DiffSinceLastSessionResult)


def test_both_none_renders_no_snapshots_available() -> None:
    result = diff_since_last_session(InMemoryStore(), None, None)
    assert result.error is None
    assert result.diff == "No snapshots available."
    assert result.cutoff_date_used is None


def test_baseline_none_latest_set_renders_not_enough_snapshots() -> None:
    result = diff_since_last_session(InMemoryStore(), None, _latest())
    assert result.error is None
    assert result.diff == "Not enough snapshots to compute a diff (need at least 2)."
    assert result.cutoff_date_used is None


def test_baseline_eq_latest_renders_real_diff_with_no_changes() -> None:
    # The kernel no longer collapses on equal versions — the
    # one-snapshot-covers-range sentinel is the adapter's responsibility.
    # Passing the same dict twice renders a real (empty) diff body.
    snap = _baseline()
    result = diff_since_last_session(InMemoryStore(), snap, snap)
    assert result.error is None
    assert result.diff is not None
    assert "Only one snapshot covers the requested time range" not in result.diff
    assert "No changes detected." in result.diff


def test_int_versions_render_two_line_version_header() -> None:
    result = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    assert result.diff is not None
    header = result.diff.split("\n", 1)[0]
    assert header == "Changes from v001 → v002"


def test_missing_versions_render_single_line_timestamp_header() -> None:
    snap_a = {
        "timestamp": "2026-05-01T10:00:00+00:00",
        "files": {"stack.md": "# Stack\n- Python 3.11\n"},
    }
    snap_b = {
        "timestamp": "2026-05-02T10:00:00+00:00",
        "files": {"stack.md": "# Stack\n- Python 3.11\n- PostgreSQL\n"},
    }
    result = diff_since_last_session(InMemoryStore(), snap_a, snap_b)
    assert result.diff is not None
    lines = result.diff.split("\n")
    assert lines[0] == "Changes from 2026-05-01T10:00:00 → 2026-05-02T10:00:00"
    assert "v0" not in lines[0]
    # The redundant second timestamp line is dropped; line 1 is the blank
    # separator the body always carries, not a parenthesised timestamp row.
    assert lines[1] == ""


def test_null_timestamp_renders_question_mark_not_typeerror() -> None:
    # A malformed-on-disk snapshot can carry an explicit ``timestamp: null`` (a
    # present key with a None value). ``.get("timestamp", "?")`` only defaults on
    # a MISSING key, so None must be handled too -- otherwise ``None[:19]`` raises
    # TypeError, which is not an OSError and escapes the CLI friendly wrapper as a
    # raw traceback.
    snap_a = {"timestamp": None, "files": {"stack.md": "# Stack\n- Python 3.11\n"}}
    snap_b = {
        "timestamp": "2026-05-02T10:00:00+00:00",
        "files": {"stack.md": "# Stack\n- Python 3.11\n- PostgreSQL\n"},
    }
    result = diff_since_last_session(InMemoryStore(), snap_a, snap_b)
    assert result.diff is not None
    assert result.diff.split("\n")[0] == "Changes from ? → 2026-05-02T10:00:00"


def test_both_populated_renders_real_diff() -> None:
    result = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    assert result.error is None
    assert result.diff is not None
    assert "v001" in result.diff
    assert "v002" in result.diff
    # Stack change surfaces as a +PostgreSQL line.
    assert "PostgreSQL" in result.diff
    # New decision file surfaces with a "New file" marker.
    assert "decisions/001-adopt-postgres.md" in result.diff


def test_state_current_sprint_transition_renders_semantically() -> None:
    # state_current.md is the real key live snapshots use. The Sprint
    # alpha → beta transition must route through _diff_state and render as a
    # semantic field delta, not degrade to a generic line-count. Asserting on
    # the rendered transition catches the dead state-diff branch.
    result = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    assert result.diff is not None
    assert "Sprint: 'alpha' → 'beta'" in result.diff
    # The generic line-count fallback must NOT fire for the state file.
    assert "Content changed" not in result.diff


def test_legacy_state_md_key_still_diffs_semantically() -> None:
    # Snapshots predating the state_current.md rename keyed state under
    # state.md. The legacy alias must still route through _diff_state so old
    # snapshots diff semantically rather than collapsing to a line-count.
    snap_a = _snapshot(
        1,
        "2026-05-01T10:00:00+00:00",
        {"state.md": "# Current State\n\n**Sprint:** alpha\n"},
    )
    snap_b = _snapshot(
        2,
        "2026-05-02T10:00:00+00:00",
        {"state.md": "# Current State\n\n**Sprint:** beta\n"},
    )
    result = diff_since_last_session(InMemoryStore(), snap_a, snap_b)
    assert result.diff is not None
    assert "Sprint: 'alpha' → 'beta'" in result.diff
    assert "Content changed" not in result.diff


def test_anchor_line_present_when_cutoff_supplied() -> None:
    cutoff = "2026-04-24T10:00:00+00:00"
    result = diff_since_last_session(
        InMemoryStore(), _baseline(), _latest(), cutoff_date_used=cutoff
    )
    assert result.diff is not None
    lines = result.diff.split("\n")
    anchor = next((line for line in lines if line.startswith("Anchor:")), None)
    assert anchor is not None
    # The requested cutoff and the resolved baseline timestamp both surface,
    # along with the selection rule.
    assert f"requested ≤ {cutoff}" in anchor
    assert "resolved to baseline 2026-05-01T10:00:00" in anchor
    assert "most-recent snapshot at-or-before cutoff" in anchor
    assert "oldest-snapshot fallback" in anchor


def test_no_arg_diff_is_byte_identical_without_anchor_line() -> None:
    # When cutoff_date_used is None (the no-arg session diff), output must be
    # byte-identical to the pre-anchor rendering: no Anchor line at all.
    with_cutoff = diff_since_last_session(
        InMemoryStore(),
        _baseline(),
        _latest(),
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    without_cutoff = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    assert without_cutoff.diff is not None
    assert "Anchor:" not in without_cutoff.diff
    # The only structural difference vs. the cutoff variant is the inserted
    # Anchor line; dropping it recovers the no-arg body exactly.
    assert with_cutoff.diff is not None
    stripped = "\n".join(
        line for line in with_cutoff.diff.split("\n") if not line.startswith("Anchor:")
    )
    assert stripped == without_cutoff.diff


def test_anchor_line_renders_on_versionless_snapshots() -> None:
    # Remote/cloud snapshots carry no integer version. The anchor must render
    # off the baseline timestamp alone — never raise, never print a bogus
    # version — and coexist with the single-line timestamp header.
    snap_a = {
        "timestamp": "2026-05-01T10:00:00+00:00",
        "files": {"stack.md": "# Stack\n- Python 3.11\n"},
    }
    snap_b = {
        "timestamp": "2026-05-02T10:00:00+00:00",
        "files": {"stack.md": "# Stack\n- Python 3.11\n- PostgreSQL\n"},
    }
    cutoff = "2026-04-24T10:00:00+00:00"
    result = diff_since_last_session(InMemoryStore(), snap_a, snap_b, cutoff_date_used=cutoff)
    assert result.diff is not None
    lines = result.diff.split("\n")
    assert lines[0] == "Changes from 2026-05-01T10:00:00 → 2026-05-02T10:00:00"
    anchor = next((line for line in lines if line.startswith("Anchor:")), None)
    assert anchor is not None
    assert f"requested ≤ {cutoff}" in anchor
    assert "resolved to baseline 2026-05-01T10:00:00" in anchor
    # No version token leaks into the anchor on the versionless shape.
    assert "v0" not in anchor


def test_cutoff_date_used_threaded_only_when_supplied() -> None:
    result_with = diff_since_last_session(
        InMemoryStore(),
        _baseline(),
        _latest(),
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    assert result_with.cutoff_date_used == "2026-04-24T10:00:00+00:00"

    result_without = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    assert result_without.cutoff_date_used is None


def test_cutoff_date_used_flows_through_on_sentinel_branches() -> None:
    result = diff_since_last_session(
        InMemoryStore(),
        None,
        None,
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    assert result.cutoff_date_used == "2026-04-24T10:00:00+00:00"


def test_result_is_frozen() -> None:
    result = diff_since_last_session(InMemoryStore(), None, None)
    with pytest.raises(ValidationError):
        result.diff = "reassigned"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DiffSinceLastSessionResult(diff="x", unexpected_field="value")


def test_exclude_none_strips_unset_fields_on_sentinel() -> None:
    result = diff_since_last_session(InMemoryStore(), None, None)
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {"diff": "No snapshots available."}


def test_exclude_none_keeps_cutoff_when_supplied() -> None:
    result = diff_since_last_session(
        InMemoryStore(),
        None,
        None,
        cutoff_date_used="2026-04-24T10:00:00+00:00",
    )
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "diff": "No snapshots available.",
        "cutoff_date_used": "2026-04-24T10:00:00+00:00",
    }


def test_store_field_absent_from_result_model_dump() -> None:
    result = diff_since_last_session(InMemoryStore(), _baseline(), _latest())
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# --- Import-graph negative constraint ---
#
# The kernel must not depend on snapshot discovery primitives — those
# sit in the local package (filesystem) and the cloud package (S3) and
# are explicitly outside the locked Store protocol. Pinning the
# constraint as an AST scan keeps the no-drift-by-construction guarantee
# from accidentally regressing through a casual import.


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "diff_since_last_session.py"
    )
    tree = ast.parse(module_path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add(f"{module}.{alias.name}")
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    return imported


def test_kernel_does_not_import_snapshot_discovery() -> None:
    imported = _kernel_imports()
    forbidden = {"list_snapshots", "load_snapshot", "find_snapshot_near_date"}
    assert forbidden.isdisjoint(imported), (
        f"kernel imports snapshot discovery primitives: {forbidden & imported}; "
        "those live in the adapter, not the kernel."
    )
