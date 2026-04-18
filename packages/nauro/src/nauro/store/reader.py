"""Store reader — read operations for the project store.

All reads from the .nauro/ project store go through this module.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nauro_core.context import build_l0, build_l1, build_l2
from nauro_core.decision_model import Decision, DecisionStatus
from nauro_core.parsing import (
    parse_decision,
)
from nauro_core.search import bm25_search

from nauro.constants import (
    DECISIONS_DIR,
    OPEN_QUESTIONS_MD,
    PROJECT_MD,
    STACK_MD,
    STATE_CURRENT_FILENAME,
    STATE_DIFF_FIELDS,
    STATE_FIELD_LAST_SYNCED_BOLD,
    STATE_HISTORY_FILENAME,
    STATE_MD,
)
from nauro.store.snapshot import find_snapshot_near_date, list_snapshots, load_snapshot


def _read_file(path: Path) -> str:
    """Read a file, return empty string if missing."""
    if path.exists():
        return path.read_text()
    return ""


def _list_decisions(store_path: Path) -> list[Decision]:
    """Parse all decision files, return ``Decision`` objects sorted by number."""
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    results: list[Decision] = []
    for f in sorted(decisions_dir.glob("*.md")):
        content = f.read_text()
        results.append(parse_decision(content, f.name))
    return results


def list_active_decisions(store_path: Path) -> list[Decision]:
    """Return only decisions with status=active."""
    return [d for d in _list_decisions(store_path) if d.status is DecisionStatus.active]


def search_decisions(
    store_path: Path,
    query: str,
    limit: int = 10,
) -> dict:
    """Search decisions by keyword across titles and rationale text.

    Case-insensitive substring matching. Multi-word queries match if ANY
    word appears in title or rationale. Includes superseded decisions.
    Results sorted by decision number descending (most recent first).

    Args:
        store_path: Path to the project store directory.
        query: Search text (must be non-empty).
        limit: Maximum results to return (default 10).

    Returns:
        Dict with store indicator, results list, total_matches, and query.
    """
    if not query or not query.strip():
        return {
            "store": "local",
            "error": (
                "search_decisions requires a non-empty query."
                " Use list_decisions to browse all decisions."
            ),
        }

    all_decisions = _list_decisions(store_path)
    results = bm25_search(all_decisions, query, limit=limit)

    return {
        "store": "local",
        "results": results,
        "total_matches": len(results),
        "query": query,
    }


def get_decision_history(store_path: Path, decision_id: str) -> list[Decision]:
    """Follow the supersedes/superseded_by chain for a decision.

    Returns a list of decisions in chronological order (oldest first).
    """
    all_decisions = _list_decisions(store_path)
    decision_map: dict[str, Decision] = {}
    decisions_dir = store_path / DECISIONS_DIR
    for d in all_decisions:
        decision_map[f"{d.num:03d}"] = d
        for f in decisions_dir.glob(f"{d.num:03d}-*.md"):
            decision_map[f.stem] = d

    target: Decision | None = None
    for key, d in decision_map.items():
        if key == decision_id or key.startswith(decision_id):
            target = d
            break

    if not target:
        return []

    chain: list[Decision] = [target]
    seen = {target.num}
    current = target
    while current.supersedes:
        prev = decision_map.get(current.supersedes)
        if prev and prev.num not in seen:
            chain.insert(0, prev)
            seen.add(prev.num)
            current = prev
        else:
            break

    current = target
    while current.superseded_by:
        nxt = decision_map.get(current.superseded_by)
        if nxt and nxt.num not in seen:
            chain.append(nxt)
            seen.add(nxt.num)
            current = nxt
        else:
            break

    return chain


def read_project_context(store_path: Path, level: int = 0) -> str:
    """Read and assemble context at the given tier level.

    L0 (concise): state + stack summary + top 5 questions + last 3 decisions
    L1 (working set): full stack + last 10 decisions + full questions
    L2 (full): all decisions + full questions + snapshot diff

    Args:
        store_path: Path to the project store directory.
        level: Context tier (0, 1, or 2).

    Returns:
        Assembled context string.
    """
    if level == 0:
        return _build_l0_local(store_path)
    elif level == 1:
        return _build_l1_local(store_path)
    else:
        return _build_l2_local(store_path)


def _load_files(store_path: Path, include_project: bool = True) -> dict[str, str]:
    """Load store files into a dict for nauro_core context builders.

    Prefers state_current.md; falls back to state.md for pre-upgrade stores.
    """
    files: dict[str, str] = {}
    if include_project:
        files["project.md"] = _read_file(store_path / PROJECT_MD)

    current_state = _read_file(store_path / STATE_CURRENT_FILENAME)
    if current_state:
        files["state_current.md"] = current_state
    else:
        files["state.md"] = _read_file(store_path / STATE_MD)

    files["stack.md"] = _read_file(store_path / STACK_MD)
    files["questions.md"] = _read_file(store_path / OPEN_QUESTIONS_MD)
    return files


def _build_l0_local(store_path: Path) -> str:
    """Build L0 payload with local-specific behavior.

    Local L0 omits project.md (included via AGENTS.md instead) and
    appends a "last synced" line from state.md.
    """
    files = _load_files(store_path, include_project=False)
    decisions = _list_decisions(store_path)

    result = build_l0(files, decisions)

    # Local-specific: append "last synced" line from state
    state = files.get("state_current.md") or files.get("state.md", "")
    synced = re.search(STATE_FIELD_LAST_SYNCED_BOLD, state)
    if synced:
        result += f"\n\n*Last synced: {synced.group(1).strip()}*"

    return result


def _build_l1_local(store_path: Path) -> str:
    """Build L1 payload — delegates to nauro_core."""
    files = _load_files(store_path, include_project=True)
    decisions = _list_decisions(store_path)
    return build_l1(files, decisions)


def _build_l2_local(store_path: Path) -> str:
    """Build L2 payload with local-specific snapshot diff."""
    files = _load_files(store_path)
    # L2 includes state history
    history = _read_file(store_path / STATE_HISTORY_FILENAME)
    if history:
        files["state_history.md"] = history
    decisions = _list_decisions(store_path)

    result = build_l2(files, decisions)

    # Local-specific: append snapshot diff
    snapshots = list_snapshots(store_path)
    if len(snapshots) >= 2:
        prev = load_snapshot(store_path, snapshots[1]["version"])
        curr = load_snapshot(store_path, snapshots[0]["version"])
        diff_lines = _file_level_diff(prev, curr)
        if diff_lines:
            diff_section = (
                f"## Snapshot Diff (v{prev['version']:03d} → v{curr['version']:03d})\n\n"
                + "\n".join(diff_lines)
            )
            if result:
                result += "\n\n" + diff_section
            else:
                result = diff_section

    return result


def _file_level_diff(prev: dict, curr: dict) -> list[str]:
    """Compute a simple file-level diff between two snapshots."""
    prev_files = prev.get("files", {})
    curr_files = curr.get("files", {})
    all_keys = sorted(set(prev_files) | set(curr_files))

    lines = []
    for key in all_keys:
        if key not in prev_files:
            lines.append(f"+ Added: {key}")
        elif key not in curr_files:
            lines.append(f"- Removed: {key}")
        elif prev_files[key] != curr_files[key]:
            lines.append(f"~ Modified: {key}")
    return lines


def diff_snapshots(store_path: Path, version_a: int, version_b: int) -> str:
    """Compare two snapshots and produce a human-readable semantic diff.

    Shows what changed in each file: new decisions, state changes,
    new questions, stack changes.

    Args:
        store_path: Path to the project store directory.
        version_a: Earlier snapshot version.
        version_b: Later snapshot version.

    Returns:
        Human-readable diff string.

    Raises:
        FileNotFoundError: If either snapshot doesn't exist.
    """
    snap_a = load_snapshot(store_path, version_a)
    snap_b = load_snapshot(store_path, version_b)

    files_a = snap_a.get("files", {})
    files_b = snap_b.get("files", {})
    all_keys = sorted(set(files_a) | set(files_b))

    sections = []
    sections.append(f"Changes from v{version_a:03d} → v{version_b:03d}")
    sections.append(
        f"  ({snap_a.get('timestamp', '?')[:19]} → {snap_b.get('timestamp', '?')[:19]})"
    )
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
            # Summarize new file content
            summary = _summarize_new_file(key, content_b)
            if summary:
                sections.append(f"    {summary}")
            continue

        if key not in files_b:
            sections.append(f"  - Removed file: {key}")
            continue

        # File was modified — produce semantic diff
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
        # Extract decision title
        for line in content.split("\n"):
            if line.startswith("# "):
                return line.lstrip("# ").strip()
        return ""
    return ""


def _semantic_file_diff(filename: str, old: str, new: str) -> list[str]:
    """Produce semantic change descriptions for a modified file."""
    changes = []

    if filename == STATE_MD:
        changes.extend(_diff_state(old, new))
    elif filename == STACK_MD:
        changes.extend(_diff_stack(old, new))
    elif filename == OPEN_QUESTIONS_MD:
        changes.extend(_diff_questions(old, new))
    elif filename.startswith(DECISIONS_DIR + "/"):
        changes.extend(_diff_decision(old, new))
    else:
        # Generic: just note it was modified
        old_lines = len(old.strip().split("\n"))
        new_lines = len(new.strip().split("\n"))
        changes.append(f"Content changed ({old_lines} → {new_lines} lines)")

    return changes


def _diff_state(old: str, new: str) -> list[str]:
    """Diff state.md semantically."""
    changes = []

    def extract_field(content: str, field: str) -> str:
        m = re.search(rf"\*\*{field}:\*\*\s*(.*)", content)
        return m.group(1).strip() if m else ""

    for field in STATE_DIFF_FIELDS:
        old_val = extract_field(old, field)
        new_val = extract_field(new, field)
        if old_val != new_val:
            changes.append(f"{field}: {old_val!r} → {new_val!r}")

    # Check recently completed items
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
    changes = []

    def extract_bullets(content: str) -> list[str]:
        return [
            line.strip()
            for line in content.split("\n")
            if line.strip().startswith("- ") and not line.startswith("  ")
        ]

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
    changes = []

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
    return ["Decision content was modified"]


def diff_since_last_session(store_path: Path, days: int | None = None) -> str:
    """Diff between snapshots, either session-scoped or time-based.

    When days is omitted: diff between the latest snapshot and the one before it
    (session-scoped, original behavior).

    When days is provided: diff between the nearest snapshot to N days ago and
    the latest snapshot.

    Args:
        store_path: Path to the project store directory.
        days: Optional number of days to look back. When provided, finds the
            nearest snapshot to N days ago and diffs against the latest.

    Returns:
        Human-readable diff string, or a message if insufficient snapshots.
    """
    snapshots = list_snapshots(store_path)

    if days is not None:
        # Time-based diff
        if not snapshots:
            return "No snapshots available."

        target = datetime.now(UTC) - timedelta(days=days)
        baseline = find_snapshot_near_date(store_path, target)
        if baseline is None:
            return "No snapshots available."

        latest = snapshots[0]["version"]
        if baseline["version"] == latest:
            return "Only one snapshot covers the requested time range — no diff available."

        return diff_snapshots(store_path, baseline["version"], latest)

    # Session-scoped (original behavior)
    if len(snapshots) < 2:
        return "Not enough snapshots to compute a diff (need at least 2)."

    latest = snapshots[0]["version"]
    previous = snapshots[1]["version"]
    return diff_snapshots(store_path, previous, latest)
