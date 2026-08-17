"""Cloud-to-store pull-and-merge over the presign transport.

Shared by ``nauro sync`` (the pull half of pull-then-push) and the SessionStart
hook. Both fetch the server manifest, diff it against sync state, mint presigned
GET URLs, transfer changed files from S3, classify every untracked decision file
before it lands, and merge conflicting append-only files. The two callers differ
only in how they surface progress, injected through the shared ``Reporter``
protocol rather than branched inside the pull core.

A run has two phases. It plans and fetches under the store's sync lock, then
writes under the decision lock as well, so a local decision writer cannot mint a
number into the middle of the batch. Nothing in the write phase touches the
network, so that writer waits for a few file writes rather than a transfer. Every
untracked decision file goes through the classifier, not only the ones that
looked like collisions at planning time.
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

    The digest is the one the ETag comparison computed, never a fresh one: between
    two reads a local writer can land an edit, and recording the second digest
    against the server's ETag would file that edit as already synced.
    """

    rel: str
    etag: str
    local_sha256: str


@dataclass(frozen=True)
class _Transfer:
    """One fetched manifest entry, ready to be applied to the store.

    A streamed transfer carries its bytes and is written before the next is fetched,
    so one body is live at a time. A spooled transfer carries a path instead, so the
    gated decision batch costs one file's memory rather than the whole batch's.
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

    ``refused`` is a subject the next sync retries: a local write error, a failed
    fetch, a missing presign URL. ``skipped_permanent`` is the opposite, and no retry
    changes it. Both count only manifest rows this run was asked to install.
    """

    merged: int = 0
    adopted: int = 0
    refused: int = 0
    skipped_permanent: int = 0
    origin_aborted: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class PullReport:
    """What one pull run left behind, for a caller that must act on it.

    ``manifest_read`` is False when the manifest fetch failed and the run never learned
    what the server holds; a presign failure counts every planned file in ``refused``,
    which a later sync retries. Both exit nonzero; ``skipped_permanent`` never does.
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

    ``decisions`` holds every decision file the store does not track, each classified
    again against the live corpus before it is written. ``adopted`` needs no bytes
    from the server and is absent from :meth:`all_files`, which the run fetches.
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

    Separators are normalised and the join is resolved through symlinks, not just the string.
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

    Shared by the planner and the write barrier; only a tracked decision still on disk is exempt.
    """
    if not (_is_decision_path(rel) or destination.inside_decisions):
        return False
    if not is_canonical_decision_path(rel):
        return True
    return rel not in state.files or not destination.exists


def _is_decision_path(rel: str) -> bool:
    """True for anything the manifest places under the decisions directory.

    Wider than what may be written, because a miss falls to the untyped write path.
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
    The unchanged-remote shortcut needs the local file present, so a deleted tracked
    file is reinstalled; a decision adopts through the classifier, never here.
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

    Planning only: it decides what to fetch, never what to write.
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
    Returns a :class:`PullReport` carrying any transport failure, never an empty
    success; exceeding ``lock_timeout`` on either lock raises ``SyncLockTimeoutError``.
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

    Every early exit ends here, so a run that fetched nothing still names what it owes.
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
    An ``OSError`` is reported against ``subject``, counted refused on ``tally``, and
    the batch carries on, which is safe only because each step leaves a healable state.
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

    Runs first and never raises: hygiene must not cost the caller the sync it asked for.
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
    """Mint one GET URL per planned file, or ``None`` on a request failure.

    A shortfall is reported here as a total; each missing file is named where consumed.
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

    A file that yields nothing is counted refused here: the caller only sees arrivals.
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

    Inside the store, under the prefix ``should_skip`` excludes, so a stranded one is never synced.
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

    Names what it could not fetch but counts nothing; the caller counts the refusals.
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

    What it declines to touch is warned about but never counted: store hygiene.
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

    The last check before bytes hit disk, asking the same question the planner asked.
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

    Tracked is last-write-wins, untracked takes the server's copy; the loser is backed up.
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

    A renumber reclassifies once the local file moved; a still-contested number is quarantined.
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

    Counted permanent only after the backup lands; a failed backup counts refused.
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
