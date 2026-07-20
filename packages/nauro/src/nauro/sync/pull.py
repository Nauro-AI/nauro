"""Store ← cloud pull-and-merge over the presign transport.

Shared by ``nauro sync`` (the pull half of pull-then-push) and the
SessionStart hook (``pull_before_session``). Both callers fetch the
server manifest, diff it against sync-state, mint presigned GET URLs, and
transfer changed files directly from S3 — then renumber colliding
decisions and merge conflicting append-only files.

The two callers differ only in how they surface progress: the CLI echoes
to the terminal; the hook logs quietly and must never raise. That
asymmetry is injected through the :class:`Reporter` protocol rather than
branched inside the pull core, so the two paths cannot drift again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from nauro_core import extract_decision_number

from nauro.cli.commands.auth import AuthRefreshError
from nauro.sync.merge import (
    detect_conflict,
    resolve_conflict,
    should_skip,
)
from nauro.sync.remote import (
    PresignError,
    fetch_manifest,
    fetch_via_presigned_url,
    request_presigned_urls,
)
from nauro.sync.state import (
    compute_sha256,
    file_changed_locally,
    file_changed_remotely,
    load_state,
    save_state,
    update_file_state,
)


class Reporter(Protocol):
    """Surface for pull progress.

    The CLI implementation echoes to the terminal; the hook implementation
    logs quietly (session startup must never crash).
    """

    def info(self, msg: str) -> None:
        """Report routine progress (file written, nothing to pull)."""

    def warn(self, msg: str) -> None:
        """Report a recoverable anomaly (presign URL shortfall, bad manifest)."""


def _renumber_decision_if_collision(
    store_path: Path,
    rel: str,
    content: bytes,
) -> tuple[str, bytes]:
    """If a pulled decision file's number collides with an existing local file, renumber it.

    Returns ``(possibly_renamed_rel, possibly_updated_content)``. Non-decision
    files, files with no parseable number, an exact-filename match, or no
    collision all pass through unchanged.
    """
    if not rel.startswith("decisions/"):
        return rel, content

    filename = rel.split("/", 1)[1]
    incoming_num = extract_decision_number(filename)
    if incoming_num is None:
        return rel, content
    decisions_dir = store_path / "decisions"
    if not decisions_dir.exists():
        return rel, content

    if (decisions_dir / filename).exists():
        return rel, content

    collision = False
    for f in decisions_dir.glob("*.md"):
        n = extract_decision_number(f.name)
        if n is not None and n == incoming_num:
            collision = True
            break

    if not collision:
        return rel, content

    existing_nums = set()
    for f in decisions_dir.glob("*.md"):
        n = extract_decision_number(f.name)
        if n is not None:
            existing_nums.add(n)

    next_num = max(existing_nums) + 1 if existing_nums else 1

    slug = _strip_number_prefix(filename)
    new_filename = f"{next_num:03d}-{slug}"
    new_rel = f"decisions/{new_filename}"

    text = content.decode("utf-8", errors="replace")
    text = _rewrite_decision_heading(text, incoming_num, next_num)
    content = text.encode("utf-8")

    return new_rel, content


def _strip_number_prefix(filename: str) -> str:
    """Drop a leading ``NNN-`` decision-number prefix from a filename.

    Mirrors ``re.sub(r"^\\d+-", "", filename)``: the leading digit run plus the
    single hyphen that immediately follows it are removed. A digit run with no
    trailing hyphen is left intact.
    """
    idx = 0
    while idx < len(filename) and filename[idx].isdigit():
        idx += 1
    if idx > 0 and idx < len(filename) and filename[idx] == "-":
        return filename[idx + 1 :]
    return filename


def _rewrite_decision_heading(text: str, incoming_num: int, next_num: int) -> str:
    """Rewrite the first ``# NNN —`` / ``# NNN -`` decision H1 to the new number.

    Mirrors ``re.sub(rf"^# {incoming_num:03d}( [—-])", ..., count=1,
    flags=re.MULTILINE)``: only the first line whose start matches ``# NNN``
    followed by a space and an em-dash or hyphen is rewritten; the separator and
    the rest of the line are preserved byte-for-byte.
    """
    old_prefix = f"# {incoming_num:03d}"
    new_prefix = f"# {next_num:03d}"
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(f"{old_prefix} —") or line.startswith(f"{old_prefix} -"):
            lines[i] = new_prefix + line[len(old_prefix) :]
            break
    return "".join(lines)


def run_pull(
    project_id: str,
    store_path: Path,
    reporter: Reporter,
) -> int:
    """Pull remote changes for ``project_id`` into ``store_path``.

    Walks the server manifest, fetches every changed file in memory (so a
    colliding decision can be renumbered before it touches disk), then writes
    clean pulls and resolves conflicting append-only files. Returns the number
    of files merged.

    Caller-facing failures (manifest/presign auth-refresh or transport errors)
    are reported through ``reporter`` and map to a 0 return.
    """
    try:
        manifest = fetch_manifest(project_id)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return 0
    except PresignError as exc:
        reporter.warn(f"manifest fetch failed: {exc}")
        return 0

    state = load_state(store_path)

    pulls: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str]] = []
    for entry in manifest:
        rel = entry.get("path", "") if isinstance(entry, dict) else ""
        if not rel or should_skip(rel):
            continue
        # Server validates per-op on presign, but the manifest itself is
        # currently trusted — drop suspicious entries before they hit disk.
        if ".." in Path(rel).parts or rel.startswith("/"):
            reporter.warn(f"skipping suspicious manifest entry {rel!r}")
            continue
        remote_etag = entry.get("etag", "")
        if not file_changed_remotely(remote_etag, rel, state):
            continue

        local_file = store_path / rel
        local_changed = file_changed_locally(store_path, rel, state)
        if not local_changed:
            pulls.append((rel, remote_etag))
            continue

        local_sha = compute_sha256(local_file) if local_file.exists() else ""
        if detect_conflict(rel, state, local_sha, remote_etag):
            conflicts.append((rel, remote_etag))

    if not pulls and not conflicts:
        state.last_full_sync = datetime.now(timezone.utc).isoformat()
        save_state(store_path, state)
        reporter.info("No remote changes")
        return 0

    operations = [{"verb": "GET", "path": rel} for rel, _etag in pulls + conflicts]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return 0
    except PresignError as exc:
        reporter.warn(f"presign request failed: {exc}")
        return 0

    if len(urls) < len(operations):
        reporter.warn(f"presign returned {len(urls)} URLs for {len(operations)} ops")

    url_by_path = {
        entry["path"]: entry["url"]
        for entry in urls
        if isinstance(entry, dict) and entry.get("verb") == "GET"
    }

    merged = 0

    for rel, remote_etag in pulls:
        url = url_by_path.get(rel)
        if not url:
            continue
        try:
            remote_content = fetch_via_presigned_url(url)
        except PresignError as exc:
            reporter.warn(f"error pulling {rel}: {exc}")
            continue
        actual_rel, remote_content = _renumber_decision_if_collision(
            store_path, rel, remote_content
        )
        actual_file = store_path / actual_rel
        actual_file.parent.mkdir(parents=True, exist_ok=True)
        actual_file.write_bytes(remote_content)
        local_sha = compute_sha256(actual_file)
        update_file_state(state, actual_rel, local_sha, remote_etag)
        merged += 1

    for rel, remote_etag in conflicts:
        url = url_by_path.get(rel)
        if not url:
            continue
        try:
            remote_content = fetch_via_presigned_url(url)
        except PresignError as exc:
            reporter.warn(f"error resolving conflict for {rel}: {exc}")
            continue
        actual_rel, remote_content = _renumber_decision_if_collision(
            store_path, rel, remote_content
        )
        if actual_rel != rel:
            # Decision-number collision, not a content conflict — write as a new file.
            actual_file = store_path / actual_rel
            actual_file.parent.mkdir(parents=True, exist_ok=True)
            actual_file.write_bytes(remote_content)
            local_sha = compute_sha256(actual_file)
            update_file_state(state, actual_rel, local_sha, remote_etag)
            merged += 1
            continue

        local_file = store_path / rel
        merged_content = resolve_conflict(store_path, local_file, remote_content, rel)
        local_file.write_bytes(merged_content)
        local_sha = compute_sha256(local_file)
        update_file_state(state, rel, local_sha, remote_etag)
        merged += 1

    state.last_full_sync = datetime.now(timezone.utc).isoformat()
    save_state(store_path, state)

    if merged:
        reporter.info(f"Merged {merged} file(s) from remote")
    else:
        reporter.info("No remote changes")

    return merged


__all__ = ["Reporter", "run_pull"]
