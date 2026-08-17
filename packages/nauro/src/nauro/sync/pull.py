"""Store ← cloud pull-and-merge over the presign transport.

Shared by ``nauro sync`` (the pull half of pull-then-push) and the
SessionStart hook (``pull_before_session``). Both callers fetch the
server manifest, diff it against sync-state, mint presigned GET URLs, and
transfer changed files directly from S3 — then classify every untracked
decision file before it lands and merge conflicting append-only files.

The two callers differ only in how they surface progress: the CLI echoes
to the terminal; the hook logs quietly and must never raise. That
asymmetry is injected through the shared
:class:`~nauro.sync.transfer.Reporter` protocol rather than branched inside
the pull core, so the two paths cannot drift again.

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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path

from nauro_core import extract_decision_number
from pydantic import BaseModel, ConfigDict, ValidationError

from nauro.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR, SNAPSHOTS_DIR
from nauro.store._atomic import atomic_write_bytes, is_tmp_sibling
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
from nauro.sync.corpus import DecisionCorpus, SkipReason
from nauro.sync.etag import ContentMatch, compare_local_file
from nauro.sync.lock import CLI_SYNC_LOCK_TIMEOUT, decision_lock, sync_lock
from nauro.sync.merge import (
    SPOOL_DIR_PREFIX,
    Side,
    normalize_rel,
    resolve_conflict,
    should_skip,
)
from nauro.sync.quarantine import save_quarantine_backup
from nauro.sync.remote import (
    PresignError,
    TransferBoundaryError,
    TransferSession,
    fetch_manifest,
    fetch_via_presigned_url,
    operation_session,
    request_presigned_urls,
    urls_by_path,
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
from nauro.sync.transfer import Reporter


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
class _AdoptedFile:
    """A file already holding the server's bytes, and the digest that proved it.

    The digest is the one the ETag comparison computed, not a fresh one. It
    travels with the entry precisely so the run never hashes the file twice:
    between two reads a local writer can land an edit, and recording the second
    digest against the server's ETag would file that edit as already synced.
    """

    rel: str
    etag: str
    local_sha256: str


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
    """What one run changed, in the terms it reports.

    Two counts describe what did not land, because they answer different
    questions. ``refused`` is a subject the next sync retries: a local write
    error, a fetch that failed, a presign URL that never arrived. Each one is
    reported, none is a failure of the run, and together they are the reason a
    run cannot claim to have synced the whole store. ``skipped_permanent`` is
    the opposite - a manifest row that resolves outside the store, a path an
    ordinary pull may not write, a quarantined collision - and retrying changes
    nothing about it, so it is reported and nothing more.

    Both count manifest rows this run was asked to install, and only those.
    Local files that were already irregular are the store's own hygiene: they
    are warned about on every run and belong in neither count.
    """

    merged: int = 0
    adopted: int = 0
    refused: int = 0
    skipped_permanent: int = 0
    origin_aborted: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class PullReport:
    """What one pull run left behind, for a caller that must act on it.

    ``nauro sync`` turns unfinished work into a nonzero exit: a run that could
    not bring the store level with the server must not report success to a
    script. Unfinished has two shapes, and a count describes only one of them.
    ``refused`` counts files a later sync retries. ``manifest_read`` is False
    when the run never learned what the server holds at all, which no file
    count can stand in for - there is no denominator. ``skipped_permanent`` is
    neither: it is reported to the user and never changes an exit code,
    because no rerun resolves it.
    """

    merged: int = 0
    refused: int = 0
    skipped_permanent: int = 0
    manifest_read: bool = True
    origin_aborted: tuple[str, ...] = ()

    @property
    def left_work_behind(self) -> bool:
        """True when the store is short of the server and a rerun can close it."""
        return self.refused > 0 or not self.manifest_read

    @classmethod
    def _of(cls, tally: _Tally) -> PullReport:
        return cls(
            merged=tally.merged,
            refused=tally.refused,
            skipped_permanent=tally.skipped_permanent,
            origin_aborted=tuple(sorted(tally.origin_aborted)),
        )


@dataclass(frozen=True)
class _Manifest:
    """The server's file list, parsed once into the views triage needs."""

    entries: tuple[_ManifestEntry, ...]
    paths: frozenset[str]
    decision_numbers: frozenset[int]
    unreadable_rows: int = 0


def _parse_manifest(rows: list[dict], reporter: Reporter) -> _Manifest:
    """Parse the manifest into typed entries plus the path and number views.

    The path set answers "could the server already hold this local file?" for
    the collision classifier, so it spans every entry, including the ones
    triage skips. The number set reserves the decision numbers the server holds
    so a local renumber never mints onto one.
    """
    entries: list[_ManifestEntry] = []
    unreadable = 0
    for row in rows:
        try:
            entry = _ManifestEntry.model_validate(row)
        except ValidationError:
            reporter.warn(f"skipping unreadable manifest entry {row!r}")
            unreadable += 1
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
        unreadable_rows=unreadable,
    )


@dataclass
class _Worklists:
    """Manifest entries split by the treatment each one needs.

    ``decisions`` holds every decision file the store does not track, whatever
    it looked like at planning time; each one is classified again against the
    live corpus before it is written. ``skipped_permanent`` counts the entries
    triage refused outright, which no worklist carries and no rerun recovers.

    ``adopted`` is the one list that needs no bytes from the server: those
    files already hold what the server published, and only the record of it is
    missing. It is deliberately absent from :meth:`all_files`, which is what
    the run presigns and fetches.
    """

    decisions: list[_RemoteFile] = field(default_factory=list)
    pulls: list[_RemoteFile] = field(default_factory=list)
    conflicts: list[_RemoteFile] = field(default_factory=list)
    adopted: list[_AdoptedFile] = field(default_factory=list)
    skipped_permanent: int = 0

    def all_files(self) -> list[_RemoteFile]:
        return self.decisions + self.pulls + self.conflicts


@dataclass(frozen=True)
class _Destination:
    """Where a manifest path would actually put bytes on this machine."""

    path: Path
    inside_store: bool
    inside_decisions: bool
    is_directory: bool
    exists: bool


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
    normalized = normalize_rel(rel)
    root = Path(os.path.realpath(store_path))
    decisions_root = Path(os.path.realpath(store_path / DECISIONS_DIR))
    resolved = Path(os.path.realpath(store_path / normalized))
    return _Destination(
        path=resolved,
        inside_store=resolved != root and resolved.is_relative_to(root),
        inside_decisions=resolved.is_relative_to(decisions_root),
        is_directory=resolved.is_dir(),
        exists=resolved.exists(),
    )


def needs_decision_gate(rel: str, destination: _Destination, state: SyncState) -> bool:
    """True when this entry must be classified rather than written directly.

    The planner routes on it and the write barrier refuses on it, so the two
    cannot answer differently - which is exactly how a tracked decision once
    ended up routed to an ordinary pull that the barrier then refused, leaving
    every update after a decision's first sync stranded.

    Either the spelling says decision or the destination does; the spelling
    check is the cheap one and the destination check is the honest one. The
    exemption is narrow: a canonically spelled decision that sync state tracks
    and whose file is still on disk is an ordinary same-path update, changing
    no number and needing no verdict. A path this store cannot enumerate is
    never exempt, however it is tracked, because a legacy entry must not buy a
    file a pass around the gate.

    The file has to be there for that reasoning to hold. Once the local copy is
    gone the number it held is free, a local writer can mint a different file
    onto it, and reinstalling the tracked path without a verdict would leave two
    files claiming one number and push the duplicate. A missing decision is
    always classified.
    """
    if not (_is_decision_path(rel) or destination.inside_decisions):
        return False
    if not is_canonical_decision_path(rel):
        return True
    return rel not in state.files or not destination.exists


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


class _Route(Enum):
    """What one manifest entry needs, before any worklist is built.

    The reasoning lives in :func:`_route_entry` alone, so the collection step
    reads as a table rather than as a second copy of it.
    """

    ignore = auto()
    unusable = auto()
    gate = auto()
    install = auto()
    adopt = auto()
    conflict = auto()


def _is_canonical_snapshot_path(rel: str) -> bool:
    head, slash, tail = rel.partition("/")
    return bool(
        slash
        and tail
        and head == SNAPSHOTS_DIR
        and all(part not in {"", ".", ".."} for part in tail.split("/"))
    )


@dataclass(frozen=True)
class _Routing:
    """What one entry needs, plus whatever deciding that already established.

    ``local_sha256`` is carried on :attr:`_Route.adopt` alone, where the ETag
    comparison read the file and hashed it. Handing it to the caller is what
    keeps the adoption to one read of the bytes it records.
    """

    route: _Route
    local_sha256: str = ""


def _route_entry(
    store_path: Path, entry: _ManifestEntry, state: SyncState, reporter: Reporter
) -> _Routing:
    """Decide what one manifest entry needs, naming the ones it refuses.

    The order is the substance. Every guard about where the bytes would land
    runs before anything about whether they are wanted, so a refusal is always
    reported rather than hidden behind a shortcut. The unchanged-remote
    shortcut then applies only when the local file is still there: the remote
    store is the record for a tracked file, so an entry the user deleted
    locally is reinstalled whether or not its etag moved.

    Below the decision gate sits the other question sync state cannot answer.
    Where state has no entry for a path, it is not saying the file changed; it
    is saying nothing at all, and the conflict route below reads that silence
    as both sides having moved. So the run settles it from the bytes instead:
    an ETag that carries a content MD5 is compared against the file on disk,
    and a file that already holds what the server published is adopted rather
    than fought over. An opaque ETag compares nothing and changes nothing.

    That leg stays below the gate on purpose. A decision adopts through the
    classifier, never here, because only the classifier can see that a sibling
    already claims the same number - and adopting under that shape would ratify
    a duplicate. Here there is nothing to weigh but the bytes.
    """
    rel = entry.path
    if should_skip(rel):
        return _Routing(_Route.ignore)
    # Server validates per-op on presign, but the manifest itself is
    # currently trusted — drop suspicious entries before they hit disk.
    if ".." in Path(rel).parts or rel.startswith("/"):
        reporter.warn(f"skipping suspicious manifest entry {rel!r}")
        return _Routing(_Route.unusable)

    destination = resolve_destination(store_path, rel)
    if not destination.inside_store:
        reporter.warn(f"skipping manifest entry {rel!r}: it resolves outside the store")
        return _Routing(_Route.unusable)
    if _is_canonical_snapshot_path(rel):
        return _Routing(_Route.ignore)

    local_file = store_path / rel
    local_exists = local_file.exists()
    if local_exists and not file_changed_remotely(entry.etag, rel, state):
        return _Routing(_Route.ignore)

    if needs_decision_gate(rel, destination, state):
        return _Routing(_Route.gate)
    if destination.is_directory:
        # Nothing a manifest names as a file may land on a directory: the
        # local-change probe below would hash it and raise.
        reporter.warn(
            f"skipping manifest entry {rel!r}: it resolves to the directory {destination.path}"
        )
        return _Routing(_Route.unusable)
    if not local_exists:
        # A file the user deleted is not a conflict: a conflict protects local
        # content and there is none.
        return _Routing(_Route.install)
    comparison = compare_local_file(local_file, entry.etag)
    if comparison.match is ContentMatch.matches:
        # The two sides are the same file. Recording that is the whole of the
        # work, and it is also what makes the run converge: a store restored
        # from the cloud, or a push that crashed before its state save, would
        # otherwise re-answer this question on every pull forever. The digest
        # travels with the verdict, from the read that reached it.
        return _Routing(_Route.adopt, comparison.sha256)
    if file_changed_locally(store_path, rel, state):
        # Reaching here with a local file means the remote moved too, so both
        # sides have content the other does not - whether or not sync state
        # tracks the file. An untracked one used to match no list at all and be
        # dropped in silence, which lost the remote version without a backup.
        return _Routing(_Route.conflict)
    return _Routing(_Route.install)


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
        routing = _route_entry(store_path, entry, state, reporter)
        route = routing.route
        if route is _Route.ignore:
            continue
        if route is _Route.unusable:
            work.skipped_permanent += 1
            continue
        if route is _Route.adopt:
            work.adopted.append(_AdoptedFile(entry.path, entry.etag, routing.local_sha256))
            continue

        item = _RemoteFile(entry.path, entry.etag)
        if route is _Route.gate:
            number = extract_decision_number(_decision_name(entry.path))
            claimed = number is not None and bool(listing.holders(number))
            (contested if claimed else work.decisions).append(item)
        elif route is _Route.install:
            work.pulls.append(item)
        else:
            work.conflicts.append(item)

    work.decisions[:0] = contested
    return work


def run_pull(
    project_id: str,
    store_path: Path,
    reporter: Reporter,
    *,
    lock_timeout: float = CLI_SYNC_LOCK_TIMEOUT,
    session: TransferSession | None = None,
) -> PullReport:
    """Pull remote changes for ``project_id`` into ``store_path``.

    Walks the server manifest, then applies it in two phases. Untracked
    decision files are fetched together and written under the decision lock,
    each classified immediately before its own write. Everything else streams.
    Large ordinary files are fetched and written one at a time, outside that
    lock, so the batch is never held in memory and a local decision writer
    never waits on the whole transfer.

    A file already holding the bytes the server published is neither, and is
    never fetched at all: the ETag settled it during triage, so the run records
    that and moves on.

    Returns a :class:`PullReport`. A transport or auth-refresh failure is
    reported through ``reporter`` and then carried in that report, in the terms
    the run can honestly give: a manifest fetch that failed returns
    ``manifest_read=False``, because the run never learned what the server
    holds and has no count to offer; a presign request that failed as a whole
    counts every planned file as refused, because there the total is known.
    Neither returns an empty report, which would say the store is already level
    with the server.

    The write phase is crash-safe in both directions: a local filesystem error
    on one file is reported and the batch continues, and sync state is written
    whatever happens, so what landed is always recorded even when the run does
    not reach the end.

    Raises:
        ~nauro.sync.lock.SyncLockTimeoutError: another sync held the store
            lock, or a local decision writer held the decision lock, for
            longer than ``lock_timeout``.
    """
    with operation_session(session) as active:
        with sync_lock(store_path, lock_timeout):
            return _run_pull_locked(project_id, store_path, reporter, lock_timeout, active)


def _run_pull_locked(
    project_id: str,
    store_path: Path,
    reporter: Reporter,
    lock_timeout: float,
    session: TransferSession,
) -> PullReport:
    _sweep_interrupted_writes(store_path, reporter)

    # A run that could not read the file list did not sync anything and cannot
    # say how much it missed. It reports that plainly rather than as a count of
    # zero, which the caller would read as a store already level with the
    # server. Nothing is written on the way out, so the last-full-sync stamp
    # keeps the date of the last run that did finish.
    try:
        rows = fetch_manifest(project_id, session=session)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return PullReport(manifest_read=False)
    except PresignError as exc:
        reporter.warn(f"manifest fetch failed: {exc}")
        return PullReport(
            manifest_read=False,
            origin_aborted=_aborted_origins(exc),
        )

    manifest = _parse_manifest(rows, reporter)
    state = load_state(store_path)
    work = _triage(store_path, manifest, state, reporter)
    tally = _Tally(skipped_permanent=manifest.unreadable_rows + work.skipped_permanent)

    planned = work.all_files()
    urls = _presign(project_id, planned, reporter, session, tally)
    if urls is None:
        # The request failed as a whole, so every file it was for stays where
        # it is. Here the denominator is known, so each one is counted: the
        # cause is reported above and the total is reported below.
        tally.refused += len(planned)
        return _report_tally(tally, reporter)

    # Flipped the moment the decision lock is held, which is the moment this run
    # can start changing the store. Everything before it - the manifest, the
    # triage, the presign, the transfers, the wait for the lock - leaves the
    # store exactly as it found it, and a failure there must not rewrite sync
    # state: a state file that failed to parse loads as empty, and persisting
    # that emptiness would untrack a whole store on a lock timeout.
    mutating = False
    try:
        # The gated batch has to be complete before the lock is taken, because
        # the lock must not span a transfer. It is spooled to disk rather than
        # held: a manifest is server-supplied input, and its total size is not a
        # number this process gets to be surprised by.
        with _spool(store_path) as spool:
            gated = _spool_batch(urls, work.decisions, reporter, spool, session, tally)
            with decision_lock(store_path, lock_timeout):
                mutating = True
                corpus = DecisionCorpus.scan(store_path)
                with _guarded_step(DECISIONS_DIR, _COMPLETION_FAILURE_DETAIL, reporter, tally):
                    _report_completion(corpus, reporter)
                for item in work.decisions:
                    transfer = gated.get(item.rel)
                    if transfer is None:
                        # No URL, or a fetch that failed - both already warned
                        # about, and both counted here rather than where they
                        # happened so one absent decision is counted once.
                        tally.refused += 1
                        continue
                    with _guarded_step(item.rel, _WRITE_FAILURE_DETAIL, reporter, tally):
                        _apply_decision(corpus, transfer, state, manifest, reporter, tally)

        for adopted in work.adopted:
            # No fetch, no write, and no second read of the file: these already
            # hold the bytes the server published, and triage kept the digest
            # that proved it. Nothing here can fail, so nothing here is guarded.
            update_file_state(state, adopted.rel, adopted.local_sha256, adopted.etag)
            tally.adopted += 1

        for transfer in _stream(urls, work.pulls, reporter, tally, session):
            with _guarded_step(transfer.rel, _WRITE_FAILURE_DETAIL, reporter, tally):
                if _generic_write_allowed(store_path, transfer, state, reporter):
                    target = store_path / transfer.rel
                    atomic_write_bytes(target, transfer.content)
                    update_file_state(state, transfer.rel, compute_sha256(target), transfer.etag)
                    tally.merged += 1
                else:
                    tally.skipped_permanent += 1

        for transfer in _stream(urls, work.conflicts, reporter, tally, session):
            with _guarded_step(transfer.rel, _WRITE_FAILURE_DETAIL, reporter, tally):
                if _generic_write_allowed(store_path, transfer, state, reporter):
                    _resolve_and_record(
                        store_path, transfer, state, _conflict_side(transfer.rel, state)
                    )
                    tally.merged += 1
                else:
                    tally.skipped_permanent += 1

        if not tally.refused:
            # The stamp says every file this run found is on disk. Only a
            # transient refusal holds it back - a local write error, a fetch
            # that failed, a presign URL that never arrived - because only
            # those come back on the next run. A permanently skipped entry is
            # reported on its own and never resolves by retrying, so gating on
            # one would freeze the stamp for good. The stamp is pull-scoped: a
            # push failure is the command's own exit-1 outcome and never
            # reaches this line.
            state.last_full_sync = datetime.now(timezone.utc).isoformat()
    finally:
        # Sync state records what actually landed, so once this run could have
        # landed something it is written whatever happens above. A run that
        # stopped early leaves a truthful partial record rather than none at
        # all, which is what keeps the next run from replaying into a store the
        # state no longer describes.
        if mutating:
            save_state(store_path, state)

    return _report_tally(tally, reporter)


def _report_tally(tally: _Tally, reporter: Reporter) -> PullReport:
    """Say what the run did, and hand the caller the same answer.

    Every leg that stops early ends here too, so a run that fetched nothing
    still names what it owes instead of falling silent.
    """
    if tally.adopted:
        reporter.info(f"Adopted {tally.adopted} local file(s) already on the server")
    if tally.merged:
        reporter.info(f"Merged {tally.merged} file(s) from remote")
    if tally.skipped_permanent:
        reporter.info(
            f"Skipped {tally.skipped_permanent} item(s) this sync cannot install: "
            "each one is named above, and another run changes nothing"
        )
    if tally.refused:
        # Said last, after the warnings that explain each one. A run that could
        # not finish must never sign off as "No remote changes".
        reporter.info(
            f"Left {tally.refused} item(s) for the next sync: this run could not "
            "fetch or write them"
        )
    elif not (tally.merged or tally.adopted or tally.skipped_permanent):
        reporter.info("No remote changes")

    return PullReport._of(tally)


# What a step that could not write says for itself. Same shape as _SKIP_DETAIL
# above - what happened to this one subject, and what the user does about it -
# because each message is printed on its own and speaks only for the file or
# the pass it names.
_WRITE_FAILURE_DETAIL = (
    "a local filesystem error stopped the write ({error}), so nothing was recorded for "
    "it and anything half-applied was left for the next run to finish. Fix the local "
    "problem - permissions, free space, a directory that disappeared - then run "
    "'nauro sync' again; it retries this file."
)
_NO_URL_DETAIL = (
    "{rel}: the server minted no download URL for it, so this run could not fetch it. "
    "It stays for the next sync."
)
_COMPLETION_FAILURE_DETAIL = (
    "the pass that finishes an interrupted renumber could not write here ({error}), so "
    "any half-applied rename was left as it was. Fix the local problem - permissions, "
    "free space - then run 'nauro sync' again; the pass runs on every sync."
)


@contextmanager
def _guarded_step(subject: str, detail: str, reporter: Reporter, tally: _Tally) -> Iterator[None]:
    """Run one mutation step, turning a local filesystem error into its outcome.

    A pull mutates many files under one lock, and a store the run cannot write
    - a read-only decision, a full disk, a directory removed underfoot - is a
    property of one path, not of the batch. Before this guard, that path's
    ``OSError`` escaped the whole run, so every file already written went
    unrecorded and the next run replayed into a store its own state no longer
    described.

    So an ``OSError`` here is reported against the subject that raised it and
    the batch carries on with the next file. That is only safe because every
    step it wraps leaves a state the next run heals rather than a half-written
    one: the remote bytes land through an atomic replace, so a file is either
    its old version or its new one and never a truncation the push would then
    upload over the good remote copy; an interrupted renumber is completed by
    the next pull's completion pass; and a file written without a state entry
    is adopted on its next classification.

    Failures are counted on the tally, because a run that could not write
    everything it found has not fully synced the store.
    """
    try:
        yield
    except OSError as exc:
        tally.refused += 1
        reporter.warn(f"{subject}: {detail.format(error=exc)}")


# How old a tmp sibling must be before the sweep treats it as garbage. An
# atomic write lives for the length of one write, so a minute-old tmp file is
# one a kill signal stranded. The floor is what keeps the sweep off a write in
# progress: store writers take the decision lock, but the store is a directory
# the user can also write into, and this runs while only the sync lock is held.
_STRANDED_TMP_MIN_AGE_SECONDS = 60.0


def _sweep_interrupted_writes(store_path: Path, reporter: Reporter) -> int:
    """Remove the tmp siblings killed writes stranded, and say how many.

    Nothing reads these files and both sync directions exclude them, so without
    a sweep they accumulate for the life of the store, each one a full copy of
    whatever was being written. The pull is where they are collected because it
    already holds the sync lock and already walks the store.

    Hygiene never costs the caller the sync it asked for. This is the first
    thing a run does, before the manifest fetch, so an error escaping it would
    abort a pull that had not started - and on the session-start hook, which
    swallows everything, skip it in silence. The walk is guarded as well as
    each file: the store is a directory other processes and the user write
    into, so a directory can vanish underneath the traversal, and which errors
    a walk raises rather than swallows differs across the Python versions this
    supports. The guarantee has to be this function's, not the walk's.
    """
    cutoff = time.time() - _STRANDED_TMP_MIN_AGE_SECONDS
    removed = 0
    try:
        for path in store_path.rglob("*"):
            if not is_tmp_sibling(path.name) or not path.is_file():
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
            except OSError as exc:
                # One dropping this run cannot remove is not a reason to refuse
                # the pull that was asked for.
                reporter.warn(f"could not remove the interrupted write {path}: {exc}")
                continue
            removed += 1
    except OSError as exc:
        # The walk stops where it broke. What it removed before that still
        # counts, and the next run starts the sweep again.
        reporter.warn(f"could not finish looking for interrupted writes: {exc}")
    if removed:
        reporter.info(f"Cleaned {removed} interrupted write(s)")
    return removed


def _presign(
    project_id: str,
    planned: list[_RemoteFile],
    reporter: Reporter,
    session: TransferSession,
    tally: _Tally,
) -> dict[str, str] | None:
    """Mint one GET URL per planned file, or None on a request failure.

    A shortfall and an unreadable entry are reported here as the totals they
    are. The file each one costs is named and counted where that file is
    consumed, so no file is counted twice and none goes unnamed.
    """
    if not planned:
        return {}

    operations = [{"verb": "GET", "path": item.rel} for item in planned]
    try:
        urls = request_presigned_urls(project_id, operations, session=session)
    except AuthRefreshError as exc:
        reporter.warn(str(exc))
        return None
    except PresignError as exc:
        tally.origin_aborted.update(_aborted_origins(exc))
        reporter.warn(f"presign request failed: {exc}")
        return None

    if len(urls) < len(operations):
        reporter.warn(f"presign returned {len(urls)} URLs for {len(operations)} ops")

    usable, skipped = urls_by_path(urls, "GET")
    for entry in skipped:
        reporter.warn(f"skipping unreadable presign entry {entry}")
    return usable


def _stream(
    urls: dict[str, str],
    items: list[_RemoteFile],
    reporter: Reporter,
    tally: _Tally,
    session: TransferSession,
) -> Iterator[_Transfer]:
    """Yield each planned file's bytes, fetched one at a time.

    The caller writes each one before the next is fetched, so only a single
    file's content is ever held. A planned file that yields nothing - no URL,
    or a transfer that failed - is named and counted refused here, because the
    caller only ever sees what arrived.
    """
    for item in items:
        url = urls.get(item.rel)
        if not url:
            reporter.warn(_NO_URL_DETAIL.format(rel=item.rel))
            tally.refused += 1
            continue
        try:
            fetched = fetch_via_presigned_url(url, session=session)
        except PresignError as exc:
            tally.origin_aborted.update(_aborted_origins(exc))
            reporter.warn(f"error pulling {item.rel}: {exc}")
            tally.refused += 1
            continue
        yield _Transfer(rel=item.rel, etag=item.etag, _body=fetched.body)


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
    urls: dict[str, str],
    items: list[_RemoteFile],
    reporter: Reporter,
    spool: Path,
    session: TransferSession,
    tally: _Tally,
) -> dict[str, _Transfer]:
    """Fetch a batch onto disk, keyed by store-relative path.

    Names what it could not fetch but counts nothing: the caller counts one
    refusal per decision missing from the batch, whichever leg lost it.
    """
    spooled: dict[str, _Transfer] = {}
    for index, item in enumerate(items):
        url = urls.get(item.rel)
        if not url:
            reporter.warn(_NO_URL_DETAIL.format(rel=item.rel))
            continue
        try:
            fetched = fetch_via_presigned_url(url, session=session)
        except PresignError as exc:
            tally.origin_aborted.update(_aborted_origins(exc))
            reporter.warn(f"error pulling {item.rel}: {exc}")
            continue
        content = fetched.body
        body = spool / f"{index:06d}"
        body.write_bytes(content)
        del content
        spooled[item.rel] = _Transfer(rel=item.rel, etag=item.etag, _spooled=body)
    return spooled


def _aborted_origins(error: PresignError) -> tuple[str, ...]:
    if not isinstance(error, TransferBoundaryError):
        return ()
    if not error.aborts_origin:
        return ()
    return (error.origin,)


_SKIP_DETAIL: dict[SkipReason, str] = {
    SkipReason.not_a_regular_file: (
        "not a regular file (a directory or a symlink). Nauro never reads or changes "
        "it, and holds back any remote decision claiming its number. Remove it or "
        "replace it with a regular file."
    ),
    SkipReason.unreadable_name: (
        "not a name Nauro reads - decision files end in a lowercase '.md'. It holds "
        "back any remote decision claiming its number. Rename it to the lowercase "
        "suffix so it becomes a decision again."
    ),
}


def _report_completion(corpus: DecisionCorpus, reporter: Reporter) -> None:
    """Run the crash-window completion pass and report what it did.

    What it declines to touch - a heading it cannot realign, a heading outside
    the form it rewrites, a decision file it never reads - is a local file that
    was already there, not something this run was asked to install. Each one is
    warned about and none is counted: they are the store's own hygiene, and
    folding them into the run's counts would make every sync report leftovers.
    """
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
        reporter.warn(f"decisions/{entry.name}: {_SKIP_DETAIL[entry.reason]}")
    if outcome.retargeted_stems:
        reporter.info(
            f"Repointed {len(outcome.retargeted_stems)} decision-hash entr"
            f"{'y' if len(outcome.retargeted_stems) == 1 else 'ies'} at renamed files"
        )


def _generic_write_allowed(
    store_path: Path, transfer: _Transfer, state: SyncState, reporter: Reporter
) -> bool:
    """Refuse an untyped write that would land somewhere it may not.

    The last check before bytes hit the disk, and the one that does not depend
    on having predicted the spelling. It asks the same question the planner
    asked, so reaching here with an entry that needed the gate means the two
    disagreed, and the write is the wrong place to resolve that.
    """
    destination = resolve_destination(store_path, transfer.rel)
    if not destination.inside_store:
        reporter.warn(
            f"refusing to write {transfer.rel!r}: it resolves to {destination.path}, "
            "outside the store"
        )
        return False
    if destination.is_directory:
        reporter.warn(
            f"refusing to write {transfer.rel!r}: it resolves to the directory {destination.path}"
        )
        return False
    if needs_decision_gate(transfer.rel, destination, state):
        reporter.warn(
            f"refusing to write {transfer.rel!r}: it resolves to {destination.path}, "
            "which is not a path an ordinary pull may write"
        )
        return False
    return True


def _conflict_side(rel: str, state: SyncState) -> Side:
    """Which copy of a contested path stays on disk.

    A tracked path's two versions descend from a copy this store published, so
    last-write-wins keeps the one the user is looking at. An untracked path was
    never published from here and has no shared ancestor: the server's copy is
    the record for it, and the local bytes go to the backup directory instead.
    Neither side is lost either way; only which one needs a rename to recover.
    """
    return Side.local if rel in state.files else Side.remote


def _resolve_and_record(
    store_path: Path, transfer: _Transfer, state: SyncState, keeps: Side
) -> None:
    """Apply the conflict policy for one path and record the result."""
    local_file = store_path / transfer.rel
    merged_content = resolve_conflict(
        store_path, local_file, transfer.content, transfer.rel, keeps=keeps
    )
    atomic_write_bytes(local_file, merged_content)
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
            _quarantine(store_path, transfer, verdict.quarantined_as(exc.reason), reporter, tally)
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
                tally,
            )
            return

    if verdict.outcome is DecisionOutcome.quarantine:
        _quarantine(store_path, transfer, verdict, reporter, tally)
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
        # The local decision stays, whatever sync state says about the path: the
        # file on disk is byte-unchanged, so the corpus still describes it and
        # the number it holds does not move underneath this batch.
        _resolve_and_record(store_path, transfer, state, Side.local)
        tally.merged += 1
        return

    target = store_path / transfer.rel
    atomic_write_bytes(target, transfer.content)
    update_file_state(state, transfer.rel, compute_sha256(target), transfer.etag)
    corpus.record_added(target, transfer.content.decode("utf-8", errors="replace"))
    tally.merged += 1


def _quarantine(
    store_path: Path,
    transfer: _Transfer,
    verdict: DecisionVerdict,
    reporter: Reporter,
    tally: _Tally,
) -> None:
    """Leave both sides alone, back up the remote bytes, and say so.

    Counted permanent once the backup is safe: the same pull runs the same way
    tomorrow, and the quarantine has its own surface in ``nauro sync --status``
    until a person settles the number. A backup that could not be written is a
    local write error instead, counted refused by the guard above this one, so
    the count is taken after the write rather than before it.
    """
    backup = save_quarantine_backup(store_path, transfer.rel, transfer.content, transfer.etag)
    tally.skipped_permanent += 1
    local_names = ", ".join(path.name for path in verdict.colliders)
    text = verdict.text()
    named = f" ({local_names})" if local_names else ""
    reporter.warn(
        f"Decision-number collision: remote {transfer.rel} was not installed because "
        f"{text.detail}{named}. {text.guidance} "
        f"The remote version is saved at {backup}."
    )


__all__ = ["PullReport", "run_pull"]
