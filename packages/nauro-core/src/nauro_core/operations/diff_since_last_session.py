"""``diff_since_last_session`` — semantic diff between two snapshot dicts.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`DiffSinceLastSessionResult`. Snapshot discovery (``list_snapshots``,
``load_snapshot``, ``find_snapshot_near_date``) sits outside the locked
Store protocol; the adapter assembles the baseline/latest snapshot dicts
and threads them in.

The diff helpers operate on the in-memory snapshot file dicts only, so
they belong with the kernel: zero filesystem dependency, deterministic
output. The ``store`` argument is retained for kernel-shape uniformity
with the other operations even though the diff body itself does not
touch it.
"""

from __future__ import annotations

from nauro_core.constants import (
    DECISIONS_DIR,
    OPEN_QUESTIONS_MD,
    STACK_MD,
    STATE_CURRENT_FILENAME,
    STATE_DIFF_FIELDS,
    STATE_MD,
)
from nauro_core.operations.results import DiffSinceLastSessionResult
from nauro_core.operations.store import Store
from nauro_core.parsing import _is_top_level_bullet

# Success-path sentinels rendered when the adapter cannot supply a usable
# (baseline, latest) pair. Exposed as module constants so adapters can
# render them at their early-exit points without re-stringifying — keeps
# the messaging the single source of truth shared by the kernel and the
# adapters that short-circuit before calling in.
NO_SNAPSHOTS_AVAILABLE = "No snapshots available."
NOT_ENOUGH_SNAPSHOTS = "Not enough snapshots to compute a diff (need at least 2)."
ONE_SNAPSHOT_COVERS_RANGE = (
    "Only one snapshot covers the requested time range — no diff available."
)
# Day-range anchor line. A format template (interpolates two runtime values)
# rather than a plain sentinel, but kept here beside the sibling sentinels so
# the wording stays single-sourced: the hosted adapter (mcp-server) imports
# this to render the same line, byte-for-byte, on the remote surface.
ANCHOR_LINE_TEMPLATE = (
    "Anchor: requested ≤ {cutoff}; resolved to baseline {baseline} "
    "(most-recent snapshot at-or-before cutoff; oldest-snapshot fallback)"
)


def diff_since_last_session(
    store: Store,
    baseline_snapshot: dict | None,
    latest_snapshot: dict | None,
    cutoff_date_used: str | None = None,
) -> DiffSinceLastSessionResult:
    """Compute a semantic diff between two snapshot dicts.

    Args:
        store: Storage adapter. Retained for kernel-shape uniformity; the
            diff body operates entirely on the supplied snapshot dicts.
        baseline_snapshot: Earlier snapshot dict as returned by
            ``load_snapshot`` (carries ``version``, ``timestamp``,
            ``files``). ``None`` signals the adapter had no usable
            baseline (zero or one snapshot in the session-scoped case).
        latest_snapshot: Later snapshot dict, same shape. ``None`` signals
            no snapshots at all.
        cutoff_date_used: When the adapter resolved the baseline via a
            time-based lookup, the requested cutoff (``now - N days``) is
            threaded through here so callers can render it. This is the
            cutoff the caller asked for, not the (possibly older) baseline
            snapshot's timestamp the lookup resolved to.

    Returns:
        :class:`DiffSinceLastSessionResult`. ``diff`` carries the
        rendered diff body. For the empty/insufficient and
        one-snapshot-covers-range cases the adapter renders the canonical
        sentinel strings exposed by this module and short-circuits before
        calling in; the kernel itself only handles the
        ``(None, *)``/``(*, None)`` sentinels and the "diff two dicts"
        path. ``error`` stays unset — the sentinel paths are normal
        success-path results, not errors.
    """
    # ``store`` is part of the locked kernel signature (see PRs 1-6); the
    # diff body operates on the supplied snapshot dicts only.
    del store

    if latest_snapshot is None or baseline_snapshot is None:
        # Adapters short-circuit these cases at their early exit; the
        # branch stays here so kernel-only callers still receive a
        # well-formed Result rather than crashing on a None lookup.
        diff = (
            NO_SNAPSHOTS_AVAILABLE
            if latest_snapshot is None and baseline_snapshot is None
            else NOT_ENOUGH_SNAPSHOTS
        )
        return DiffSinceLastSessionResult(diff=diff, cutoff_date_used=cutoff_date_used)

    return DiffSinceLastSessionResult(
        diff=_render_diff(baseline_snapshot, latest_snapshot, cutoff_date_used),
        cutoff_date_used=cutoff_date_used,
    )


def _render_diff(snap_a: dict, snap_b: dict, cutoff_date_used: str | None = None) -> str:
    """Render the semantic diff body between two snapshot dicts."""
    # Version numbers come from the snapshot dict itself, not a caller-supplied
    # integer like the pre-cutover ``diff_snapshots(store_path, version_a,
    # version_b)``. Snapshots that carry no integer ``version`` (e.g. the
    # versionless remote shape) fall back to a single timestamp-only header.
    version_a = snap_a.get("version")
    version_b = snap_b.get("version")
    ts_a = (snap_a.get("timestamp") or "?")[:19]
    ts_b = (snap_b.get("timestamp") or "?")[:19]
    files_a = snap_a.get("files", {})
    files_b = snap_b.get("files", {})
    all_keys = sorted(set(files_a) | set(files_b))

    sections = []
    if isinstance(version_a, int) and isinstance(version_b, int):
        sections.append(f"Changes from v{version_a:03d} → v{version_b:03d}")
        sections.append(f"  ({ts_a} → {ts_b})")
    else:
        sections.append(f"Changes from {ts_a} → {ts_b}")
    # Day-range path only: surface the requested cutoff against the resolved
    # baseline so the (silent, age-degrading) anchor fuzz is visible. Keyed off
    # cutoff_date_used and the baseline TIMESTAMP only — never an integer
    # version, which versionless remote snapshots do not carry. Absent for the
    # no-arg session diff, keeping that output byte-identical.
    if cutoff_date_used is not None:
        sections.append(ANCHOR_LINE_TEMPLATE.format(cutoff=cutoff_date_used, baseline=ts_a))
    sections.append("")

    has_changes = False

    for key in all_keys:
        content_a = files_a.get(key, "")
        content_b = files_b.get(key, "")

        if content_a == content_b:
            continue

        has_changes = True

        if key not in files_a:
            sections.append(f"  + New file: {key}")
            summary = _summarize_new_file(key, content_b)
            if summary:
                sections.append(f"    {summary}")
            continue

        if key not in files_b:
            sections.append(f"  - Removed file: {key}")
            continue

        sections.append(f"  ~ {key}")
        changes = _semantic_file_diff(key, content_a, content_b)
        for change in changes:
            sections.append(f"    {change}")

    if not has_changes:
        sections.append("  No changes detected.")

    return "\n".join(sections)


def _summarize_new_file(filename: str, content: str) -> str:
    """Produce a one-line summary for a new file."""
    if filename.startswith(DECISIONS_DIR + "/"):
        for line in content.split("\n"):
            if line.startswith("# "):
                return line.lstrip("# ").strip()
        return ""
    return ""


def _semantic_file_diff(filename: str, old: str, new: str) -> list[str]:
    """Produce semantic change descriptions for a modified file."""
    changes: list[str] = []

    if filename in (STATE_CURRENT_FILENAME, STATE_MD):
        # Live snapshots store current state under state_current.md; old
        # snapshots predating the rename used state.md, kept as a legacy
        # alias so they still diff semantically.
        changes.extend(_diff_state(old, new))
    elif filename == STACK_MD:
        changes.extend(_diff_stack(old, new))
    elif filename == OPEN_QUESTIONS_MD:
        changes.extend(_diff_questions(old, new))
    elif filename.startswith(DECISIONS_DIR + "/"):
        changes.extend(_diff_decision(old, new))
    else:
        old_lines = len(old.strip().split("\n"))
        new_lines = len(new.strip().split("\n"))
        changes.append(f"Content changed ({old_lines} → {new_lines} lines)")

    return changes


def _diff_state(old: str, new: str) -> list[str]:
    """Diff state.md semantically."""
    changes: list[str] = []

    def extract_field(content: str, field: str) -> str:
        marker = f"**{field}:**"
        for line in content.splitlines():
            idx = line.find(marker)
            if idx >= 0:
                return line[idx + len(marker) :].strip()
        return ""

    for field in STATE_DIFF_FIELDS:
        old_val = extract_field(old, field)
        new_val = extract_field(new, field)
        if old_val != new_val:
            changes.append(f"{field}: {old_val!r} → {new_val!r}")

    old_items = [
        line.strip()
        for line in old.split("\n")
        if line.strip().startswith("- ") and "none yet" not in line
    ]
    new_items = [
        line.strip()
        for line in new.split("\n")
        if line.strip().startswith("- ") and "none yet" not in line
    ]
    added = [i for i in new_items if i not in old_items]
    for item in added:
        changes.append(f"+ Completed: {item.lstrip('- ')}")

    return changes


def _diff_stack(old: str, new: str) -> list[str]:
    """Diff stack.md semantically."""
    changes: list[str] = []

    def extract_bullets(content: str) -> list[str]:
        return [line.strip() for line in content.split("\n") if _is_top_level_bullet(line)]

    old_bullets = extract_bullets(old)
    new_bullets = extract_bullets(new)

    added = [b for b in new_bullets if b not in old_bullets]
    removed = [b for b in old_bullets if b not in new_bullets]

    for b in added:
        changes.append(f"+ {b}")
    for b in removed:
        changes.append(f"- {b}")

    if not changes:
        changes.append("Stack details updated")

    return changes


def _diff_questions(old: str, new: str) -> list[str]:
    """Diff open-questions.md semantically."""
    changes: list[str] = []

    def extract_questions(content: str) -> list[str]:
        return [line.strip() for line in content.split("\n") if line.strip().startswith("- [")]

    old_qs = extract_questions(old)
    new_qs = extract_questions(new)

    added = [q for q in new_qs if q not in old_qs]
    removed = [q for q in old_qs if q not in new_qs]

    for q in added:
        changes.append(f"+ New question: {q.lstrip('- ')}")
    for q in removed:
        changes.append(f"- Resolved/removed: {q.lstrip('- ')}")

    return changes


def _diff_decision(old: str, new: str) -> list[str]:
    """Diff a decision file (decisions are generally immutable, so just note changes)."""
    del old, new
    return ["Decision content was modified"]
