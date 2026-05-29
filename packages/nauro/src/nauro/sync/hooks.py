"""Event-driven sync hooks — pull on session start, push after writes.

Called by the MCP server (``stdio_server._pull_on_startup`` on entry,
``mcp/tools._try_push`` after each write). Never block or crash —
failures are logged only.

Both hooks gate on Auth0 token presence and v2 cloud-mode at entry and
silent-no-op when either is missing. The two no-op cases are:

* Not authenticated. MCP writes happen on every tool call; nagging
  ``run nauro auth login`` on every write would be hostile. The user
  saw the prompt at session start (or onboarding) — here we just skip.
* Project is not v2 cloud-mode. v1 entries have no server-side ULID
  and v2 local-mode is not remote-backed. The presign endpoints can
  address neither.

Token refresh on 401 is handled inside ``request_presigned_urls`` and
``fetch_manifest`` via ``with_token_refresh``. ``AuthRefreshError``
escapes here as a swallowed log line.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from nauro_core import extract_decision_number

from nauro.cli.commands.auth import AuthRefreshError, load_access_token
from nauro.store.registry import is_cloud_project

logger = logging.getLogger("nauro.sync")


def _renumber_decision_if_collision(
    store_path: Path,
    rel: str,
    content: bytes,
) -> tuple[str, bytes]:
    """If a pulled decision file's number collides with an existing local file, renumber it.

    Returns (possibly_renamed_rel, possibly_updated_content).
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

    slug = re.sub(r"^\d+-", "", filename)
    new_filename = f"{next_num:03d}-{slug}"
    new_rel = f"decisions/{new_filename}"

    text = content.decode("utf-8", errors="replace")
    text = re.sub(
        rf"^# {incoming_num:03d}( [—-])",
        f"# {next_num:03d}\\1",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    content = text.encode("utf-8")

    logger.info(
        "Renumbered pulled decision %s → %s to avoid collision with local decision %03d",
        filename,
        new_filename,
        incoming_num,
    )
    return new_rel, content


def pull_before_session(project_id: str, store_path: Path) -> int:
    """Pull remote changes from the server before a session starts.

    Silent no-op when not authenticated or when ``project_id`` is not a
    v2 cloud-mode entry. Returns the number of files pulled/merged, or
    0 on any swallowed failure. Never raises — auto-pull must not crash
    session startup.
    """
    if not load_access_token():
        return 0
    if not is_cloud_project(project_id):
        return 0

    try:
        from nauro.sync.merge import (
            UnionMergeError,
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
    except ImportError:
        return 0

    try:
        manifest = fetch_manifest(project_id)
    except AuthRefreshError as exc:
        logger.warning("sync pull: %s", exc)
        return 0
    except PresignError as exc:
        logger.warning("sync pull: manifest fetch failed: %s", exc)
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
            logger.warning("sync pull: skipping suspicious manifest entry %r", rel)
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
        return 0

    operations = [{"verb": "GET", "path": rel} for rel, _etag in pulls + conflicts]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        logger.warning("sync pull: %s", exc)
        return 0
    except PresignError as exc:
        logger.warning("sync pull: presign request failed: %s", exc)
        return 0

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
        except PresignError:
            logger.exception("sync pull: error pulling %s", rel)
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
        except PresignError:
            logger.exception("sync pull: error resolving conflict for %s", rel)
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
        else:
            local_file = store_path / rel
            try:
                merged_content = resolve_conflict(
                    store_path, local_file, remote_content, rel, state
                )
            except UnionMergeError:
                # Leave the local file untouched rather than overwrite it with
                # possibly-corrupt bytes. Session startup must never crash.
                logger.exception("sync pull: union merge failed for %s", rel)
                continue
            local_file.write_bytes(merged_content)
            local_sha = compute_sha256(local_file)
            update_file_state(state, rel, local_sha, remote_etag)
        merged += 1

    state.last_full_sync = datetime.now(timezone.utc).isoformat()
    save_state(store_path, state)

    if merged:
        logger.info("sync pull: merged %d file(s) for %s", merged, project_id)

    return merged


def push_after_write(project_id: str, store_path: Path) -> int:
    """Push changed local files after a write (decision, question, state).

    Silent no-op when not authenticated or when ``project_id`` is not a
    v2 cloud-mode entry. Returns the number of files pushed, or 0 on any
    swallowed failure. Never raises — auto-push must not surface errors
    on every MCP tool call.
    """
    if not load_access_token():
        return 0
    if not is_cloud_project(project_id):
        return 0

    try:
        from nauro.sync.merge import should_skip
        from nauro.sync.remote import (
            PresignError,
            put_via_presigned_url,
            request_presigned_urls,
        )
        from nauro.sync.state import (
            compute_sha256,
            file_changed_locally,
            load_state,
            save_state,
            update_file_state,
        )
    except ImportError:
        return 0

    state = load_state(store_path)

    changed: list[tuple[str, str, Path]] = []
    for local_file in store_path.rglob("*"):
        if not local_file.is_file():
            continue
        try:
            rel = str(local_file.relative_to(store_path))
        except ValueError:
            continue
        if should_skip(rel) or rel.startswith(".conflict-backup") or rel.startswith("__pycache__"):
            continue
        if not file_changed_locally(store_path, rel, state):
            continue
        local_sha = compute_sha256(local_file)
        changed.append((rel, local_sha, local_file))

    if not changed:
        save_state(store_path, state)
        return 0

    operations = [{"verb": "PUT", "path": rel} for rel, _sha, _path in changed]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        logger.warning("sync push: %s", exc)
        return 0
    except PresignError as exc:
        logger.warning("sync push: presign request failed: %s", exc)
        return 0

    url_by_path = {
        entry["path"]: entry["url"]
        for entry in urls
        if isinstance(entry, dict) and entry.get("verb") == "PUT"
    }

    pushed = 0
    for rel, local_sha, local_file in changed:
        url = url_by_path.get(rel)
        if not url:
            continue
        try:
            new_etag = put_via_presigned_url(url, local_file)
        except PresignError:
            logger.exception("sync push: failed to push %s", rel)
            continue
        if new_etag:
            update_file_state(state, rel, local_sha, new_etag)
            pushed += 1

    save_state(store_path, state)

    if pushed:
        logger.info("sync push: pushed %d file(s) for %s", pushed, project_id)

    return pushed
