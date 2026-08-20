"""Snapshot capture and management.

Snapshots are point-in-time JSON captures of the full project store,
written to snapshots/vNNN.json. Pruned with logarithmic spacing:
- Last 7 days: keep every snapshot
- Last 30 days: keep one per day
- Last 6 months: keep one per week
- Older than 6 months: keep one per month

Every read path works from one scan (`_scan_snapshots`) that retains only
scalar headers; whole bodies load only in `load_snapshot`.
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock
from nauro_core.snapshot import serialize_snapshot

from nauro.constants import (
    DECISIONS_DIR,
    PRUNE_DAILY_DAYS,
    PRUNE_KEEP_ALL_DAYS,
    PRUNE_WEEKLY_DAYS,
    SNAPSHOTS_DIR,
)
from nauro.store._atomic import atomic_write_text
from nauro.store.reader import read_text_lenient

# Exceptions that mean a snapshot file on disk is unreadable or malformed: a
# truncated/garbage JSON body, a missing required key, or an OS read error. The
# read paths skip such a file with a warning rather than letting it crash the
# command (log/sync/diff) on a single bad snapshot.
_CORRUPT_SNAPSHOT_ERRORS = (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError)

logger = logging.getLogger("nauro.snapshot")


@dataclass(frozen=True)
class _SnapshotMeta:
    """The scalar header of one snapshot file; the body stays on disk.

    ``timestamp`` is ``None`` when the user-editable raw value does not parse;
    such a snapshot is listed and versioned but never pruned or date-matched.
    """

    path: Path
    version: int
    raw_timestamp: str
    timestamp: datetime | None
    trigger: str
    trigger_detail: str
    token_count: int


def _parse_timestamp(raw: str) -> datetime | None:
    """The snapshot timestamp as an aware datetime, ``None`` when unusable.

    Naive values count as unusable: they cannot compare against UTC cutoffs.
    """
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _scan_snapshots(snapshots_dir: Path) -> list[_SnapshotMeta]:
    """Parse every canonical vNNN.json into a scalar header, oldest version first.

    Skips unreadable or misnamed files with a warning; no body is retained past the scan.
    """
    metas = []
    for f in snapshots_dir.glob("v*.json"):
        try:
            data = json.loads(f.read_text())
            version = data["version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise TypeError(f"bad version {version!r}")
            raw_timestamp = str(data["timestamp"])
            meta = _SnapshotMeta(
                path=f,
                version=version,
                raw_timestamp=raw_timestamp,
                timestamp=_parse_timestamp(raw_timestamp),
                trigger=str(data.get("trigger", "")),
                trigger_detail=str(data.get("trigger_detail", "")),
                token_count=int(data.get("token_count") or 0),
            )
        except _CORRUPT_SNAPSHOT_ERRORS:
            logger.warning("Skipping unreadable snapshot: %s", f)
            continue
        # Only the canonical name for the body's version may enter retention: a
        # mismatch (v001-copy.json, a hand-renamed file) must not steer version
        # derivation and must never reach the prune delete set.
        if f.name != f"v{meta.version:03d}.json":
            logger.warning("Skipping non-canonical snapshot name: %s", f)
            continue
        metas.append(meta)
    # Sort numerically by the version field, never lexically by filename:
    # v999.json sorts after v1000.json as a string, which would make capture
    # re-derive version 1000 and overwrite it.
    metas.sort(key=lambda m: m.version)
    return metas


@contextmanager
def _snapshot_lock(snapshots_dir: Path):
    """Exclusive file lock on the snapshots dir for atomic capture.

    Spans read-compute-write: the next version is derived from the existing snapshots.
    """
    lock_path = snapshots_dir / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        yield


def capture_snapshot(store_path: Path, trigger: str = "", trigger_detail: str = "") -> int:
    """Capture every markdown file in the store as one JSON snapshot.

    Returns the auto-incremented version number.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Hold the lock across compute-version, write, and prune. Locking only the
    # write would leave the read-compute race open: two captures would read the
    # same existing version, derive the same next version, and one would
    # overwrite the other.
    with _snapshot_lock(snapshots_dir):
        metas = _scan_snapshots(snapshots_dir)
        next_version = max((m.version for m in metas), default=0) + 1

        # Read all markdown files
        files = {}
        for md in sorted(store_path.glob("*.md")):
            files[md.name] = read_text_lenient(md)

        decisions_dir = store_path / DECISIONS_DIR
        if decisions_dir.exists():
            for md in sorted(decisions_dir.glob("*.md")):
                files[f"{DECISIONS_DIR}/{md.name}"] = read_text_lenient(md)

        now = datetime.now(timezone.utc)
        snapshot = serialize_snapshot(
            timestamp=now.isoformat(),
            trigger=trigger,
            trigger_detail=trigger_detail,
            files=files,
            version=next_version,
        )

        out_path = snapshots_dir / f"v{next_version:03d}.json"
        atomic_write_text(out_path, json.dumps(snapshot, indent=2) + "\n")

        # Prune after every capture, inside the lock, over the set just scanned
        # plus the snapshot just written — no second disk pass.
        new_meta = _SnapshotMeta(
            path=out_path,
            version=next_version,
            raw_timestamp=snapshot["timestamp"],
            timestamp=now,
            trigger=trigger,
            trigger_detail=trigger_detail,
            token_count=snapshot["token_count"],
        )
        _prune_snapshots([*metas, new_meta])

    return next_version


def _prune_snapshots(metas: list[_SnapshotMeta]) -> None:
    """Prune snapshots by age: 7 days all, 30 days daily, 6 months weekly, then monthly.

    Deletes only snapshots with a parseable timestamp; the latest version always survives.
    """
    if len(metas) <= 1:
        return

    dated = [(m.timestamp, m) for m in metas if m.timestamp is not None]
    if not dated:
        return

    keep = {max(metas, key=lambda m: m.version).path}
    keep.add(max(dated, key=lambda p: p[1].version)[1].path)

    now = datetime.now(timezone.utc)
    keep_all_cutoff = now - timedelta(days=PRUNE_KEEP_ALL_DAYS)
    daily_cutoff = now - timedelta(days=PRUNE_DAILY_DAYS)
    weekly_cutoff = now - timedelta(days=PRUNE_WEEKLY_DAYS)

    bucket_daily: dict[str, list[tuple[datetime, _SnapshotMeta]]] = {}
    bucket_weekly: dict[str, list[tuple[datetime, _SnapshotMeta]]] = {}
    bucket_monthly: dict[str, list[tuple[datetime, _SnapshotMeta]]] = {}

    for ts, m in dated:
        if m.path in keep:
            continue
        if ts >= keep_all_cutoff:
            keep.add(m.path)
        elif ts >= daily_cutoff:
            bucket_daily.setdefault(ts.strftime("%Y-%m-%d"), []).append((ts, m))
        elif ts >= weekly_cutoff:
            bucket_weekly.setdefault(ts.strftime("%Y-W%W"), []).append((ts, m))
        else:
            bucket_monthly.setdefault(ts.strftime("%Y-%m"), []).append((ts, m))

    for bucket in (bucket_daily, bucket_weekly, bucket_monthly):
        for group in bucket.values():
            newest = max(group, key=lambda p: p[0])
            keep.add(newest[1].path)

    for _ts, m in dated:
        if m.path not in keep:
            m.path.unlink()


def _nearest_at_or_before(metas: list[_SnapshotMeta], target: datetime) -> _SnapshotMeta | None:
    """The newest dated snapshot at or before ``target``.

    Falls back to the oldest dated snapshot when none predates it, ``None`` when none exist.
    """
    dated = sorted(
        ((m.timestamp, m) for m in metas if m.timestamp is not None),
        key=lambda p: p[0],
    )
    if not dated:
        return None
    candidates = [m for ts, m in dated if ts <= target]
    return candidates[-1] if candidates else dated[0][1]


def find_snapshot_near_date(store_path: Path, target: datetime) -> dict | None:
    """Return metadata for the newest snapshot at or before ``target``.

    Falls back to the oldest snapshot when none predates it, ``None`` when none exist.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    if not snapshots_dir.exists():
        return None

    best = _nearest_at_or_before(_scan_snapshots(snapshots_dir), target)
    if best is None:
        return None
    return {"version": best.version, "timestamp": best.raw_timestamp}


def list_snapshots(store_path: Path) -> list[dict]:
    """Return snapshot metadata without file contents, most recent first."""
    snapshots_dir = store_path / SNAPSHOTS_DIR
    if not snapshots_dir.exists():
        return []

    return [
        {
            "version": m.version,
            "timestamp": m.raw_timestamp,
            "trigger": m.trigger,
            "trigger_detail": m.trigger_detail,
            "token_count": m.token_count,
        }
        for m in reversed(_scan_snapshots(snapshots_dir))
    ]


def load_snapshot(store_path: Path, version: int) -> dict:
    """Load one snapshot version, file contents included.

    Raises ``FileNotFoundError`` when that version does not exist.
    """
    path = store_path / SNAPSHOTS_DIR / f"v{version:03d}.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot v{version:03d} not found.")
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def resolve_diff_snapshots(
    store_path: Path,
    days: int | None,
) -> tuple[dict | None, dict | None, str | None]:
    """Assemble the ``(baseline, latest, cutoff_date_used)`` tuple for the kernel.

    ``cutoff_date_used`` is the requested cutoff, not the resolved baseline's date.
    """
    snapshots_dir = store_path / SNAPSHOTS_DIR
    metas = _scan_snapshots(snapshots_dir) if snapshots_dir.exists() else []
    if not metas:
        return None, None, None
    latest = metas[-1]

    if days is not None:
        now = datetime.now(timezone.utc)
        # Clamp the lookback to the representable date range on both sides
        # before the subtraction. A huge positive --days reaches past the
        # earliest representable date and just means "everything" (anchor at
        # the oldest in-range instant); a huge negative --days would push the
        # target past datetime.max and overflow. In-range negatives still
        # resolve to the most-recent snapshot.
        max_days = (now - datetime.min.replace(tzinfo=timezone.utc)).days
        min_days = -((datetime.max.replace(tzinfo=timezone.utc) - now).days)
        target = now - timedelta(days=max(min(days, max_days), min_days))
        baseline = _nearest_at_or_before(metas, target)
        if baseline is None:
            return None, None, None
        return (
            load_snapshot(store_path, baseline.version),
            load_snapshot(store_path, latest.version),
            target.isoformat(),
        )

    if len(metas) < 2:
        return None, load_snapshot(store_path, latest.version), None
    return (
        load_snapshot(store_path, metas[-2].version),
        load_snapshot(store_path, latest.version),
        None,
    )
