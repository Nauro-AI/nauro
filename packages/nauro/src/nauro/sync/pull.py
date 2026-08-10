"""Store ← cloud pull-and-merge over the presign transport.

Shared by ``nauro sync`` (the pull half of pull-then-push) and the
SessionStart hook (``pull_before_session``). Both callers fetch the
server manifest, diff it against sync-state, mint presigned GET URLs, and
transfer changed files directly from S3 — then classify every untracked
decision file before it lands and merge conflicting append-only files.

The two callers differ only in how they surface progress: the CLI echoes
to the terminal; the hook logs quietly and must never raise. That
asymmetry is injected through the :class:`Reporter` protocol rather than
branched inside the pull core, so the two paths cannot drift again.

A run has two phases. It plans and fetches under the store's sync lock, which
keeps two syncs off one store; then it writes everything under the decision
lock as well, which keeps a local decision writer from minting a number into
the middle of the batch. Nothing in the write phase touches the network, so the
lock a local writer waits on is held for the length of a few file writes rather
than the length of a transfer. Both acquisitions are bounded by the caller's
policy: the CLI fails loud after a bounded wait, the hook skips its pull rather
than delaying session start.

Every decision file the store does not already track is written through the
classifier, not just the ones that looked like collisions when the run planned
its transfers - two remote files can claim one number, and a local writer can
mint one between the plan and the write.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from nauro_core import extract_decision_number
from pydantic import BaseModel, ConfigDict, ValidationError

from nauro.cli.commands.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR
from nauro.sync.collisions import (
    DecisionOutcome,
    DecisionVerdict,
    QuarantineReason,
    RenumberRefusedError,
    apply_canonicalize,
    apply_renumber,
    classify_decision,
    is_canonical_decision_path,
    run_completion_pass,
)
from nauro.sync.corpus import DecisionCorpus
from nauro.sync.lock import CLI_SYNC_LOCK_TIMEOUT, decision_lock, sync_lock
from nauro.sync.merge import (
    SPOOL_DIR_PREFIX,
    detect_conflict,
    resolve_conflict,
    should_skip,
)
from nauro.sync.quarantine import save_quarantine_backup
from nauro.sync.remote import (
    PresignError,
    fetch_manifest,
    fetch_via_presigned_url,
    request_presigned_urls,
)
from nauro.sync.state import (
    SyncState,
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


class _ManifestEntry(BaseModel):
    """One server manifest row, as far as the pull core reads it."""

    model_config = ConfigDict(extra="ignore")

    path: str
    etag: str = ""


@dataclass(frozen=True)
class _RemoteFile:
    """One manifest entry the pull decided to transfer."""

    rel: str
    etag: str


@dataclass(frozen=True)
class _Transfer:
    """One fetched manifest entry, ready to be applied to the store.

    A streamed transfer carries its bytes: it is written before the next one is
    fetched, so only one body is ever live. A spooled transfer carries a path
    instead and reads the body back when asked, so the gated decision batch -
    which must be fetched in full before the lock is taken - costs one file's
    memory rather than the whole batch's.
    """

    rel: str
    etag: str
    _body: bytes | None = None
    _spooled: Path | None = None

    @property
    def content(self) -> bytes:
        if self._spooled is not None:
            return self._spooled.read_bytes()
        assert self._body is not None  # one of the two is always set
        return self._body


@dataclass
class _Tally:
    """What one run changed, in the terms it reports."""

    merged: int = 0
    adopted: int = 0


@dataclass(frozen=True)
class _Manifest:
    """The server's file list, parsed once into the views triage needs."""

    entries: tuple[_ManifestEntry, ...]
    paths: frozenset[str]
    decision_numbers: frozenset[int]


def _parse_manifest(rows: list[dict], reporter: Reporter) -> _Manifest:
    """Parse the manifest into typed entries plus the path and number views.

    The path set answers "could the server already hold this local file?" for
    the collision classifier, so it spans every entry, including the ones
    triage skips. The number set reserves the decision numbers the server holds
    so a local renumber never mints onto one.
    """
    entries: list[_ManifestEntry] = []
    for row in rows:
        try:
            entry = _ManifestEntry.model_validate(row)
        except ValidationError:
            reporter.warn(f"skipping unreadable manifest entry {row!r}")
            continue
        if entry.path:
            entries.append(entry)

    paths = {entry.path for entry in entries}
    numbers = set()
    for path in paths:
        if _is_decision_path(path):
            num = extract_decision_number(_decision_name(path))
            if num is not None:
                numbers.add(num)
    return _Manifest(
        entries=tuple(entries),
        paths=frozenset(paths),
        decision_numbers=frozenset(numbers),
    )


@dataclass
class _Worklists:
    """Manifest entries split by the treatment each one needs.

    ``decisions`` holds every decision file the store does not track, whatever
    it looked like at planning time; each one is classified again against the
    live corpus before it is written.
    """

    decisions: list[_RemoteFile] = field(default_factory=list)
    pulls: list[_RemoteFile] = field(default_factory=list)
    conflicts: list[_RemoteFile] = field(default_factory=list)

    def all_files(self) -> list[_RemoteFile]:
        return self.decisions + self.pulls + self.conflicts


@dataclass(frozen=True)
class _Destination:
    """Where a manifest path would actually put bytes on this machine."""

    path: Path
    inside_store: bool
    inside_decisions: bool
    is_directory: bool

    @property
    def writable_generically(self) -> bool:
        """True only where an untyped write may land."""
        return self.inside_store and not self.inside_decisions and not self.is_directory


def resolve_destination(store_path: Path, rel: str) -> _Destination:
    """Resolve a manifest path to the file it would really write.

    The safety question is not how the manifest spelled a path but where the
    bytes land, and the two come apart in more ways than a predicate can
    enumerate: ``./decisions/x.md`` and ``.//decisions/x.md`` normalise into
    the decisions directory, a backslash key is a separator on Windows (and the
    push scan emits them there), and a symlink can point anywhere. Separators
    are normalised the way ``should_skip`` already normalises them, then the
    join is resolved through symlinks so the answer describes the filesystem
    rather than the string.

    The decisions directory counts as inside itself. An entry named exactly
    ``decisions`` resolves to it, and treating that as an ordinary file would
    either crash on hashing a directory or, on a store that has none yet,
    create a regular file under the name every future decision write needs.
    """
    normalized = rel.replace("\\", "/")
    root = Path(os.path.realpath(store_path))
    decisions_root = Path(os.path.realpath(store_path / DECISIONS_DIR))
    resolved = Path(os.path.realpath(store_path / normalized))
    return _Destination(
        path=resolved,
        inside_store=resolved != root and resolved.is_relative_to(root),
        inside_decisions=resolved.is_relative_to(decisions_root),
        is_directory=resolved.is_dir(),
    )


def _is_decision_path(rel: str) -> bool:
    """True for anything the manifest places under the decisions directory.

    Deliberately wider than what may be written. Routing has to catch every
    spelling that could name a decision on some filesystem - a folded case
    (``Decisions/007-x.MD``), a trailing dot Windows strips
    (``decisions/007-x.md.``), a path that normalises onto a real file
    (``decisions//007-x.md``) - because the alternative for anything it misses
    is the untyped path, which writes wherever the manifest points. What may
    actually be written is decided by :func:`is_canonical_decision_path`, and
    everything else under this directory is quarantined.
    """
    head, slash, _name = rel.partition("/")
    return bool(slash) and head.casefold() == DECISIONS_DIR


def _decision_name(rel: str) -> str:
    """The filename part of a decision path, whatever the directory's spelling."""
    return rel.partition("/")[2]


def _triage(
    store_path: Path,
    manifest: _Manifest,
    state: SyncState,
    reporter: Reporter,
) -> _Worklists:
    """Split the manifest into gated decision writes, clean pulls, and conflicts.

    Planning only: it decides what to fetch, never what to write. The order it
    puts decisions in is a preference, not a verdict - entries whose number an
    existing local file already claims go first, so a collision is judged
    against the store the user has rather than one this same run has written
    unrelated files into.
    """
    work = _Worklists()
    listing = DecisionCorpus.scan(store_path)
    contested: list[_RemoteFile] = []
    for entry in manifest.entries:
        rel = entry.path
        if should_skip(rel):
            continue
        # Server validates per-op on presign, but the manifest itself is
        # currently trusted — drop suspicious entries before they hit disk.
        if ".." in Path(rel).parts or rel.startswith("/"):
            reporter.warn(f"skipping suspicious manifest entry {rel!r}")
            continue
        if not file_changed_remotely(entry.etag, rel, state):
            continue

        item = _RemoteFile(rel, entry.etag)
        destination = resolve_destination(store_path, rel)
        if not destination.inside_store:
            reporter.warn(f"skipping manifest entry {rel!r}: it resolves outside the store")
            continue
        # Either the spelling says decision or the destination does. The
        # spelling check is the cheap one and the destination check is the
        # honest one; a path only the latter recognises is exactly the kind
        # this gate exists for. The tracked-state exemption comes last, so a
        # legacy entry recorded under a spelling this store cannot enumerate
        # never buys the file a pass around the gate.
        if (_is_decision_path(rel) or destination.inside_decisions) and (
            not is_canonical_decision_path(rel) or rel not in state.files
        ):
            number = extract_decision_number(_decision_name(rel))
            claimed = number is not None and bool(listing.holders(number))
            (contested if claimed else work.decisions).append(item)
            continue

        if destination.is_directory:
            # Nothing a manifest names as a file may land on a directory: the
            # local-change probe below would hash it and raise.
            reporter.warn(
                f"skipping manifest entry {rel!r}: it resolves to the directory {destination.path}"
            )
            continue

        local_file = store_path / rel
        if not file_changed_locally(store_path, rel, state):
            work.pulls.append(item)
            continue

        local_sha = compute_sha256(local_file) if local_file.exists() else ""
        if detect_conflict(rel, state, local_sha, entry.etag):
            work.conflicts.append(item)

    work.decisions[:0] = contested
    return work


def run_pull(
    project_id: str,
    store_path: Path,
    reporter: Reporter,
    *,
    lock_timeout: float = CLI_SYNC_LOCK_TIMEOUT,
) -> int:
    """Pull remote changes for ``project_id`` into ``store_path``.

    Walks the server manifest, then applies it in two phases. Untracked
    decision files are fetched together and written under the decision lock,
    each classified immediately before its own write. Everything else streams:
    fetched and written one file at a time, outside that lock, so a store whose
    changed set is hundreds of megabytes of snapshots is never held in memory
    and a local decision writer never waits on the whole batch.

    Returns the number of files merged. Caller-facing failures
    (manifest/presign auth-refresh or transport errors) are reported through
    ``reporter`` and map to a 0 return.

    Raises:
        ~nauro.sync.lock.SyncLockTimeoutError: another sync held the store
            lock, or a local decision writer held the decision lock, for
            longer than ``lock_timeout``.
    """
    with sync_lock(store_path, lock_timeout):
        return _run_pull_locked(project_id, store_path, reporter, lock_timeout)


def _run_pull_locked(
    project_id: str, store_path: Path, reporter: Reporter, lock_timeout: float
) -> int:
    try:
        rows = fetch_manifest(project_id)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return 0
    except PresignError as exc:
        reporter.warn(f"manifest fetch failed: {exc}")
        return 0

    manifest = _parse_manifest(rows, reporter)
    state = load_state(store_path)
    work = _triage(store_path, manifest, state, reporter)

    urls = _presign(project_id, work.all_files(), reporter)
    if urls is None:
        return 0

    tally = _Tally()
    # The gated batch has to be complete before the lock is taken, because the
    # lock must not span a transfer. It is spooled to disk rather than held:
    # a manifest is server-supplied input, and its total size is not a number
    # this process gets to be surprised by.
    with _spool(store_path) as spool:
        gated = _spool_batch(urls, work.decisions, reporter, spool)
        with decision_lock(store_path, lock_timeout):
            corpus = DecisionCorpus.scan(store_path)
            _report_completion(corpus, reporter)
            for item in work.decisions:
                transfer = gated.get(item.rel)
                if transfer is not None:
                    _apply_decision(corpus, transfer, state, manifest, reporter, tally)

    for transfer in _stream(urls, work.pulls, reporter):
        if not _generic_write_allowed(store_path, transfer, reporter):
            continue
        target = store_path / transfer.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(transfer.content)
        update_file_state(state, transfer.rel, compute_sha256(target), transfer.etag)
        tally.merged += 1

    for transfer in _stream(urls, work.conflicts, reporter):
        if not _generic_write_allowed(store_path, transfer, reporter):
            continue
        _resolve_and_record(store_path, transfer, state)
        tally.merged += 1

    state.last_full_sync = datetime.now(timezone.utc).isoformat()
    save_state(store_path, state)

    if tally.adopted:
        reporter.info(f"Adopted {tally.adopted} local file(s) already on the server")
    if tally.merged:
        reporter.info(f"Merged {tally.merged} file(s) from remote")
    elif not tally.adopted:
        reporter.info("No remote changes")

    return tally.merged


def _presign(
    project_id: str, planned: list[_RemoteFile], reporter: Reporter
) -> dict[str, str] | None:
    """Mint one GET URL per planned file, or None on a request failure."""
    if not planned:
        return {}

    operations = [{"verb": "GET", "path": item.rel} for item in planned]
    try:
        urls = request_presigned_urls(project_id, operations)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return None
    except PresignError as exc:
        reporter.warn(f"presign request failed: {exc}")
        return None

    if len(urls) < len(operations):
        reporter.warn(f"presign returned {len(urls)} URLs for {len(operations)} ops")

    return {
        entry["path"]: entry["url"]
        for entry in urls
        if isinstance(entry, dict) and entry.get("verb") == "GET"
    }


def _stream(
    urls: dict[str, str], items: list[_RemoteFile], reporter: Reporter
) -> Iterator[_Transfer]:
    """Yield each planned file's bytes, fetched one at a time.

    The caller writes each one before the next is fetched, so only a single
    file's content is ever held.
    """
    for item in items:
        url = urls.get(item.rel)
        if not url:
            continue
        try:
            content = fetch_via_presigned_url(url)
        except PresignError as exc:
            reporter.warn(f"error pulling {item.rel}: {exc}")
            continue
        yield _Transfer(rel=item.rel, etag=item.etag, _body=content)


@contextmanager
def _spool(store_path: Path) -> Iterator[Path]:
    """A scratch directory for fetched decision bodies, removed on the way out.

    It lives in the store so it shares the store's filesystem, and its name
    carries the prefix ``should_skip`` excludes, so a directory left behind by
    a kill signal is never pushed or pulled.
    """
    directory = Path(tempfile.mkdtemp(prefix=SPOOL_DIR_PREFIX, dir=store_path))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _spool_batch(
    urls: dict[str, str], items: list[_RemoteFile], reporter: Reporter, spool: Path
) -> dict[str, _Transfer]:
    """Fetch a batch onto disk, keyed by store-relative path."""
    spooled: dict[str, _Transfer] = {}
    for index, item in enumerate(items):
        url = urls.get(item.rel)
        if not url:
            continue
        try:
            content = fetch_via_presigned_url(url)
        except PresignError as exc:
            reporter.warn(f"error pulling {item.rel}: {exc}")
            continue
        body = spool / f"{index:06d}"
        body.write_bytes(content)
        del content
        spooled[item.rel] = _Transfer(rel=item.rel, etag=item.etag, _spooled=body)
    return spooled


def _report_completion(corpus: DecisionCorpus, reporter: Reporter) -> None:
    """Run the crash-window completion pass and report what it did."""
    outcome = run_completion_pass(corpus)
    for repair in outcome.repaired:
        reporter.info(
            f"Completed an interrupted renumber: {repair.path.name} heading "
            f"{repair.from_num:03d} -> {repair.to_num:03d}"
        )
    for path in outcome.unrepairable:
        reporter.warn(
            f"{path.name}: its heading number and filename number disagree, and it "
            "cannot be realigned because another file claims that number, another "
            "record references it, or the store could not be read. Rename the file "
            "to match its heading, or resolve the other claim first."
        )
    for path in outcome.quarantined:
        reporter.warn(
            f"{path.name}: its heading number and filename number disagree, and its "
            "heading is not in the canonical form this tool rewrites; correct the "
            "heading by hand, then run 'nauro sync' again"
        )
    for entry in corpus.irregular:
        reporter.warn(
            f"decisions/{entry.name} is not a regular file (a directory or a symlink); "
            "Nauro never reads or changes it, and holds back any remote decision "
            "claiming its number. Remove or replace it with a regular file."
        )
    if outcome.retargeted_stems:
        reporter.info(
            f"Repointed {len(outcome.retargeted_stems)} decision-hash entr"
            f"{'y' if len(outcome.retargeted_stems) == 1 else 'ies'} at renamed files"
        )


def _generic_write_allowed(store_path: Path, transfer: _Transfer, reporter: Reporter) -> bool:
    """Refuse an untyped write that would land somewhere it may not.

    The last check before bytes hit the disk, and the one that does not depend
    on having predicted the spelling: planning routes by destination too, so
    reaching here with a decision destination means the two disagreed, and the
    write is the wrong place to resolve that.
    """
    destination = resolve_destination(store_path, transfer.rel)
    if destination.writable_generically:
        return True
    reporter.warn(
        f"refusing to write {transfer.rel!r}: it resolves to {destination.path}, "
        "which is not a path an ordinary pull may write"
    )
    return False


def _resolve_and_record(store_path: Path, transfer: _Transfer, state: SyncState) -> None:
    """Apply the last-write-wins conflict policy and record the result."""
    local_file = store_path / transfer.rel
    merged_content = resolve_conflict(store_path, local_file, transfer.content, transfer.rel)
    local_file.write_bytes(merged_content)
    update_file_state(state, transfer.rel, compute_sha256(local_file), transfer.etag)


def _apply_decision(
    corpus: DecisionCorpus,
    transfer: _Transfer,
    state: SyncState,
    manifest: _Manifest,
    reporter: Reporter,
    tally: _Tally,
) -> None:
    """Classify one untracked remote decision and act on the verdict.

    A renumber frees the contested number without settling what happens to the
    remote file, so the verdict is taken again once the local file has moved.
    The second pass is against a number nothing claims, so it cannot ask for
    another renumber; a store that still contests the number after one move has
    more than one claim on it and is quarantined.
    """
    store_path = corpus.store_path
    verdict = classify_decision(corpus, transfer.rel, transfer.content, state, manifest.paths)
    if verdict.outcome is DecisionOutcome.renumber_local:
        number = extract_decision_number(_decision_name(transfer.rel))
        assert number is not None  # a renumber verdict names a numbered collider
        try:
            renumber = apply_renumber(corpus, verdict.collider, number, manifest.decision_numbers)
        except RenumberRefusedError as exc:
            # Refused before anything was renamed or written, and the refusal
            # names its own reason rather than borrowing the heading's.
            reporter.warn(str(exc))
            _quarantine(store_path, transfer, verdict.quarantined_as(exc.reason), reporter)
            return
        reporter.info(
            f"Renumbered unpublished local decision {renumber.old_num:03d} -> "
            f"{renumber.new_num:03d} ({renumber.new_path.name}) to free the "
            f"number for {transfer.rel}"
        )
        verdict = classify_decision(corpus, transfer.rel, transfer.content, state, manifest.paths)
        if verdict.outcome is DecisionOutcome.renumber_local:
            _quarantine(
                store_path,
                transfer,
                verdict.quarantined_as(QuarantineReason.ambiguous_colliders),
                reporter,
            )
            return

    if verdict.outcome is DecisionOutcome.quarantine:
        _quarantine(store_path, transfer, verdict, reporter)
        return

    if verdict.outcome is DecisionOutcome.canonicalize:
        renamed = apply_canonicalize(corpus, verdict.collider, transfer.rel)
        update_file_state(state, transfer.rel, compute_sha256(renamed), transfer.etag)
        reporter.info(
            f"Adopted local {verdict.collider.name} as {renamed.name}: "
            "same decision under two names"
        )
        tally.adopted += 1
        return

    if verdict.outcome is DecisionOutcome.adopt:
        update_file_state(state, transfer.rel, compute_sha256(verdict.collider), transfer.etag)
        tally.adopted += 1
        return

    if verdict.outcome is DecisionOutcome.resolve_conflict:
        # Last-write-wins keeps the local decision, so the file on disk is
        # byte-unchanged and the corpus still describes it.
        _resolve_and_record(store_path, transfer, state)
        tally.merged += 1
        return

    target = store_path / transfer.rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(transfer.content)
    update_file_state(state, transfer.rel, compute_sha256(target), transfer.etag)
    if target.parent == store_path / DECISIONS_DIR:
        # A manifest path that only spells the directory like decisions/ is not
        # part of the corpus this run maintains, whatever the filesystem folds.
        corpus.record_added(target, transfer.content.decode("utf-8", errors="replace"))
    tally.merged += 1


def _quarantine(
    store_path: Path,
    transfer: _Transfer,
    verdict: DecisionVerdict,
    reporter: Reporter,
) -> None:
    """Leave both sides alone, back up the remote bytes, and say so."""
    backup = save_quarantine_backup(store_path, transfer.rel, transfer.content, transfer.etag)
    local_names = ", ".join(path.name for path in verdict.colliders)
    text = verdict.text()
    named = f" ({local_names})" if local_names else ""
    reporter.warn(
        f"Decision-number collision: remote {transfer.rel} was not installed because "
        f"{text.detail}{named}. {text.guidance} "
        f"The remote version is saved at {backup}."
    )


__all__ = ["Reporter", "run_pull"]
