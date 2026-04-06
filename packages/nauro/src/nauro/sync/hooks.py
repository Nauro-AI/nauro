"""Event-driven sync hooks — pull on session start, push after extraction.

These replace the daemon's poll loop. Called from MCP server hooks
and extraction pipeline. Never block or crash — failures are logged only.
"""

import logging
import re
from pathlib import Path

from nauro_core import extract_decision_number

logger = logging.getLogger("nauro.sync")


def _renumber_decision_if_collision(
    store_path: Path, rel: str, content: bytes,
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

    # Check if this exact filename already exists locally — no collision
    if (decisions_dir / filename).exists():
        return rel, content

    # Check if another file with the same number prefix exists
    collision = False
    for f in decisions_dir.glob("*.md"):
        n = extract_decision_number(f.name)
        if n is not None and n == incoming_num:
            collision = True
            break

    if not collision:
        return rel, content

    # Find next available number
    existing_nums = set()
    for f in decisions_dir.glob("*.md"):
        n = extract_decision_number(f.name)
        if n is not None:
            existing_nums.add(n)

    next_num = max(existing_nums) + 1 if existing_nums else 1

    slug = re.sub(r"^\d+-", "", filename)
    new_filename = f"{next_num:03d}-{slug}"
    new_rel = f"decisions/{new_filename}"

    # Update the heading inside the file content (e.g. "# 075 — Title" → "# 087 — Title")
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
        filename, new_filename, incoming_num,
    )
    return new_rel, content


def pull_before_session(project_name: str, store_path: Path) -> int:
    """Pull remote changes from S3 before a session starts.

    Returns the number of files pulled/merged, or 0 on failure/no changes.
    Never raises — failures are logged and swallowed.
    """
    try:
        from nauro.sync.config import AuthRequiredError, load_sync_config, require_auth, s3_prefix
        from nauro.sync.merge import detect_conflict, resolve_conflict, should_skip
        from nauro.sync.remote import create_client, list_remote
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

    config = load_sync_config()
    if not config.enabled:
        return 0

    try:
        sanitized_sub = require_auth(config)
    except AuthRequiredError:
        logger.warning("sync pull: auth not configured — run 'nauro auth login'")
        return 0

    try:
        client = create_client(config)
        prefix = s3_prefix(sanitized_sub, project_name)
        remote_files = list_remote(client, config.bucket_name, prefix)
    except Exception:
        logger.warning("sync pull: could not reach remote for %s", project_name)
        return 0

    state = load_state(store_path)
    merged = 0

    for rf in remote_files:
        rel = rf["key"].removeprefix(prefix)
        if not rel or should_skip(rel):
            continue

        remote_etag = rf["etag"]
        remote_changed = file_changed_remotely(remote_etag, rel, state)
        if not remote_changed:
            continue

        local_file = store_path / rel
        local_changed = file_changed_locally(store_path, rel, state)

        if remote_changed and not local_changed:
            try:
                # Pull to a temp location first so we can check for decision number collisions
                response = client.get_object(Bucket=config.bucket_name, Key=prefix + rel)
                remote_content = response["Body"].read()
                remote_etag = rf["etag"]

                actual_rel, remote_content = _renumber_decision_if_collision(
                    store_path, rel, remote_content,
                )
                actual_file = store_path / actual_rel
                actual_file.parent.mkdir(parents=True, exist_ok=True)
                actual_file.write_bytes(remote_content)

                local_sha = compute_sha256(actual_file)
                update_file_state(state, actual_rel, local_sha, remote_etag)
                merged += 1
            except Exception:
                logger.exception("sync pull: error pulling %s", rel)
        elif remote_changed and local_changed:
            local_sha = compute_sha256(local_file) if local_file.exists() else ""
            if detect_conflict(rel, state, local_sha, remote_etag):
                try:
                    response = client.get_object(Bucket=config.bucket_name, Key=prefix + rel)
                    remote_content = response["Body"].read()

                    # For decisions, check if this is actually a different decision
                    # with the same number (not a content conflict on the same file)
                    actual_rel, remote_content = _renumber_decision_if_collision(
                        store_path, rel, remote_content,
                    )
                    if actual_rel != rel:
                        # It was a number collision, not a true conflict — write as new file
                        actual_file = store_path / actual_rel
                        actual_file.parent.mkdir(parents=True, exist_ok=True)
                        actual_file.write_bytes(remote_content)
                        local_sha = compute_sha256(actual_file)
                        update_file_state(state, actual_rel, local_sha, remote_etag)
                    else:
                        merged_content = resolve_conflict(
                            store_path, local_file, remote_content, rel, state
                        )
                        local_file.write_bytes(merged_content)
                        local_sha = compute_sha256(local_file)
                        update_file_state(state, rel, local_sha, remote_etag)
                    merged += 1
                except Exception:
                    logger.exception("sync pull: error resolving conflict for %s", rel)

    from datetime import UTC, datetime

    state.last_full_sync = datetime.now(UTC).isoformat()
    save_state(store_path, state)

    if merged:
        logger.info("sync pull: merged %d file(s) for %s", merged, project_name)

    return merged


def push_after_write(project_name: str, store_path: Path) -> int:
    """Push store files to S3 after a local write (decision, question, state).

    Returns the number of files pushed, or 0 on failure/not configured.
    Never raises — failures are logged only.
    """
    try:
        from nauro.sync.config import AuthRequiredError, load_sync_config, require_auth, s3_prefix
        from nauro.sync.merge import should_skip
        from nauro.sync.remote import create_client, push_file
        from nauro.sync.state import (
            compute_sha256,
            file_changed_locally,
            load_state,
            save_state,
            update_file_state,
        )
    except ImportError:
        return 0

    config = load_sync_config()
    if not config.enabled:
        return 0

    try:
        sanitized_sub = require_auth(config)
    except AuthRequiredError:
        logger.warning("sync push: auth not configured — run 'nauro auth login'")
        return 0

    try:
        client = create_client(config)
    except Exception:
        logger.warning("sync push: could not create S3 client for %s", project_name)
        return 0

    prefix = s3_prefix(sanitized_sub, project_name)
    state = load_state(store_path)
    pushed = 0

    for local_file in store_path.rglob("*"):
        if not local_file.is_file():
            continue
        try:
            rel = str(local_file.relative_to(store_path))
        except ValueError:
            continue
        if should_skip(rel) or rel.startswith(".conflict-backup") or rel.startswith("__pycache__"):
            continue

        # Skip files that haven't changed since the last push
        if not file_changed_locally(store_path, rel, state):
            continue

        try:
            local_sha = compute_sha256(local_file)
            remote_key = prefix + rel
            new_etag = push_file(client, config.bucket_name, local_file, remote_key)
            if new_etag:
                update_file_state(state, rel, local_sha, new_etag)
                pushed += 1
        except Exception:
            logger.exception("sync push: failed to push %s", rel)

    save_state(store_path, state)

    if pushed:
        logger.info("sync push: pushed %d file(s) for %s", pushed, project_name)

    return pushed


# Backward-compatible alias — extraction pipeline still imports this name.
push_after_extraction = push_after_write
