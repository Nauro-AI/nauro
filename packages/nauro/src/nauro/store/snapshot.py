"""Snapshot capture and management.

Snapshots are point-in-time JSON captures of the full project store,
written to snapshots/vNNN.json. Pruned with logarithmic spacing:
- Last 7 days: keep every snapshot
- Last 30 days: keep one per day
- Last 6 months: keep one per week
- Older than 6 months: keep one per month

Snapshots where the decisions/ count increased are auto-pinned
and never pruned (preserves the decision chain).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nauro.constants import (
    CHARS_PER_TOKEN,
    DECISIONS_DIR,
    PRUNE_DAILY_DAYS,
    PRUNE_KEEP_ALL_DAYS,
    PRUNE_WEEKLY_DAYS,
    SCHEMA_VERSION,
    SNAPSHOTS_DIR,
)


def capture_snapshot(store_path: Path, trigger: str = "", trigger_detail: str = "") -> int:
    """Capture a snapshot of the current project context.

    Reads all markdown files in the store, bundles into a JSON object with
    auto-incremented version, timestamp, trigger, and file contents.

    Args:
        store_path: Path to the project store directory.
        trigger: Description of what triggered this snapshot.
        trigger_detail: Additional detail about the trigger.

    Returns:
        Snapshot version number.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Determine next version
    existing = list_snapshots(store_path)
    next_version = (existing[0]["version"] + 1) if existing else 1

    # Read all markdown files
    files = {}
    for md in sorted(store_path.glob("*.md")):
        files[md.name] = md.read_text()

    decisions_dir = store_path / DECISIONS_DIR
    if decisions_dir.exists():
        for md in sorted(decisions_dir.glob("*.md")):
            files[f"{DECISIONS_DIR}/{md.name}"] = md.read_text()

    # Count total characters as a rough token proxy
    token_count = sum(len(v) for v in files.values()) // CHARS_PER_TOKEN

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "version": next_version,
        "timestamp": datetime.now(UTC).isoformat(),
        "trigger": trigger,
        "trigger_detail": trigger_detail,
        "token_count": token_count,
        "files": files,
    }

    out_path = snapshots_dir / f"v{next_version:03d}.json"
    out_path.write_text(json.dumps(snapshot, indent=2) + "\n")

    # Prune after every capture
    _prune_snapshots(snapshots_dir)

    return next_version


def _count_decisions(snapshot_data: dict) -> int:
    """Count the number of decisions/ files in a snapshot."""
    return sum(1 for k in snapshot_data.get("files", {}) if k.startswith(DECISIONS_DIR + "/"))


def _prune_snapshots(snapshots_dir: Path) -> None:
    """Prune snapshots using logarithmic spacing.

    Buckets:
    1. Last 7 days: keep every snapshot
    2. Last 30 days: keep one per day (newest from each day)
    3. Last 6 months: keep one per week (newest from each week)
    4. Older than 6 months: keep one per month

    Auto-pin: any snapshot where decisions/ file count is larger than
    the previous snapshot's decisions/ count is never pruned.
    """
    snapshot_files = sorted(snapshots_dir.glob("v*.json"))
    if len(snapshot_files) <= 1:
        return

    # Load all snapshots with their metadata
    snapshots = []
    for f in snapshot_files:
        data = json.loads(f.read_text())
        snapshots.append(
            {
                "path": f,
                "data": data,
                "timestamp": datetime.fromisoformat(data["timestamp"]),
                "decision_count": _count_decisions(data),
            }
        )

    # Sort by version (chronological)
    snapshots.sort(key=lambda s: s["data"]["version"])

    # Determine pinned snapshots (decision count increased vs previous)
    pinned = set()
    for i, snap in enumerate(snapshots):
        if i == 0:
            continue
        if snap["decision_count"] > snapshots[i - 1]["decision_count"]:
            pinned.add(snap["path"])

    # Always keep the latest snapshot
    latest = snapshots[-1]["path"]

    now = datetime.now(UTC)
    keep_all_cutoff = now - timedelta(days=PRUNE_KEEP_ALL_DAYS)
    daily_cutoff = now - timedelta(days=PRUNE_DAILY_DAYS)
    weekly_cutoff = now - timedelta(days=PRUNE_WEEKLY_DAYS)

    # Assign each snapshot to a bucket and determine keepers
    keep = {latest}  # always keep latest
    keep.update(pinned)  # always keep pinned

    # Bucket snapshots (excluding latest and pinned — they're already kept)
    bucket_daily: dict[str, list] = {}  # day_key -> [snapshots]
    bucket_weekly: dict[str, list] = {}  # week_key -> [snapshots]
    bucket_monthly: dict[str, list] = {}  # month_key -> [snapshots]

    for snap in snapshots:
        if snap["path"] in keep:
            continue

        ts = snap["timestamp"]

        if ts >= keep_all_cutoff:
            # Last 7 days: keep all
            keep.add(snap["path"])
        elif ts >= daily_cutoff:
            # Last 30 days: one per day
            day_key = ts.strftime("%Y-%m-%d")
            bucket_daily.setdefault(day_key, []).append(snap)
        elif ts >= weekly_cutoff:
            # Last 6 months: one per week
            week_key = ts.strftime("%Y-W%W")
            bucket_weekly.setdefault(week_key, []).append(snap)
        else:
            # Older: one per month
            month_key = ts.strftime("%Y-%m")
            bucket_monthly.setdefault(month_key, []).append(snap)

    # From each bucket, keep the newest snapshot
    for bucket in (bucket_daily, bucket_weekly, bucket_monthly):
        for _key, snaps in bucket.items():
            newest = max(snaps, key=lambda s: s["timestamp"])
            keep.add(newest["path"])

    # Delete snapshots not in the keep set
    for snap in snapshots:
        if snap["path"] not in keep:
            snap["path"].unlink()


def find_snapshot_near_date(store_path: Path, target: datetime) -> dict | None:
    """Find the most recent snapshot that is at or before the target datetime.

    Scans all snapshots and returns the one closest to (but not after) the
    target. If no snapshot is old enough, returns the oldest available.

    Args:
        store_path: Path to the project store directory.
        target: The target datetime to search near.

    Returns:
        Snapshot metadata dict (version, timestamp), or None if no snapshots exist.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    if not snapshots_dir.exists():
        return None

    all_snaps = []
    for f in snapshots_dir.glob("v*.json"):
        data = json.loads(f.read_text())
        ts = datetime.fromisoformat(data["timestamp"])
        all_snaps.append(
            {
                "version": data["version"],
                "timestamp": data["timestamp"],
                "datetime": ts,
            }
        )

    if not all_snaps:
        return None

    all_snaps.sort(key=lambda s: s["datetime"])

    # Find the most recent snapshot at or before the target
    candidates = [s for s in all_snaps if s["datetime"] <= target]
    if candidates:
        best = candidates[-1]  # most recent one at or before target
    else:
        # No snapshot old enough — return the oldest available
        best = all_snaps[0]

    return {"version": best["version"], "timestamp": best["timestamp"]}


def list_snapshots(store_path: Path) -> list[dict]:
    """Return snapshot metadata (version, timestamp, trigger, etc.) without full content.

    Args:
        store_path: Path to the project store directory.

    Returns:
        List of metadata dicts, most recent first.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    if not snapshots_dir.exists():
        return []

    result = []
    for f in sorted(snapshots_dir.glob("v*.json"), reverse=True):
        data = json.loads(f.read_text())
        result.append(
            {
                "version": data["version"],
                "timestamp": data["timestamp"],
                "trigger": data.get("trigger", ""),
                "trigger_detail": data.get("trigger_detail", ""),
                "token_count": data.get("token_count", 0),
            }
        )
    return result


def load_snapshot(store_path: Path, version: int) -> dict:
    """Load a specific snapshot.

    Args:
        store_path: Path to the project store directory.
        version: Snapshot version number.

    Returns:
        Full snapshot dict including file contents.

    Raises:
        FileNotFoundError: If snapshot doesn't exist.
    """
    path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot v{version:03d} not found.")
    return json.loads(path.read_text())  # type: ignore[no-any-return]
