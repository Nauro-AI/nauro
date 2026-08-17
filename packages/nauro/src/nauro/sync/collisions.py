"""Classification of every decision file a pull is about to write.

A remote decision whose number a differently named local file already holds is
not a content conflict: two records claim one identity. The remote number is
shared state other machines reference, so it never moves; the local collider is
classified instead, remote bytes in hand. Canonicalize renames a byte-identical
unpublished twin onto the remote name, renumber moves a provably unpublished,
unreferenced, rewritable local file aside, and quarantine installs nothing.

Provably unpublished means no sync-state entry tracks the path and the server
manifest does not hold it. Every untracked decision write is classified at write
time, under the decision lock, against the corpus the writer keeps current. The
renumber writes rename, heading, hash index, install in that order, so each crash
window leaves a state the next pull heals. A referenced file is quarantined
rather than reference-rewritten.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nauro_core import extract_decision_number
from pydantic import BaseModel, ConfigDict, ValidationError

from nauro.constants import DECISION_HASHES_FILE, DECISIONS_DIR
from nauro.store._atomic import atomic_write_text
from nauro.store.reader import read_text_lenient
from nauro.sync.corpus import DecisionCorpus, DecisionFile
from nauro.sync.headings import (
    has_rewritable_heading,
    heading_number,
    rewrite_decision_heading,
    strip_number_prefix,
)
from nauro.sync.state import SyncState


class DecisionOutcome(str, Enum):
    """What the pull should do with one remote decision file."""

    install = "install"
    adopt = "adopt"
    resolve_conflict = "resolve-conflict"
    canonicalize = "canonicalize"
    renumber_local = "renumber-local"
    quarantine = "quarantine"


class QuarantineReason(str, Enum):
    """Why a decision could not be installed or resolved automatically."""

    tracked_locally = "tracked-locally"
    published_remotely = "published-remotely"
    ambiguous_colliders = "ambiguous-colliders"
    unparseable_collider = "unparseable-collider"
    store_unverifiable = "store-unverifiable"
    referenced = "referenced"
    question_reference = "question-reference"
    heading_not_rewritable = "heading-not-rewritable"
    irregular_entry = "irregular-entry"
    non_canonical_path = "non-canonical-path"
    case_variant_present = "case-variant-present"
    destination_occupied = "destination-occupied"


# A collider the server already holds is published on both sides, and this
# transport moves whole files by path: it cannot rename or delete a remote
# object, so no client-side sequence resolves it. Everything else is an
# unpublished local file the owner renames or removes.
_PUBLISHED_GUIDANCE = (
    "Both sides are published, so no client-side resolution exists; "
    "resolve it through the Nauro app as the project owner."
)
_LOCAL_GUIDANCE = "Rename or remove the local file, then run 'nauro sync' again."


@dataclass(frozen=True)
class _ReasonText:
    detail: str
    guidance: str


# Every reason declares its own wording, so a new one cannot inherit the wrong
# resolution route by falling through to a default.
_REASON_TEXT: dict[QuarantineReason, _ReasonText] = {
    QuarantineReason.tracked_locally: _ReasonText(
        "the local file is tracked in sync state (already published)", _PUBLISHED_GUIDANCE
    ),
    QuarantineReason.published_remotely: _ReasonText(
        "the local file's path is also in the server manifest", _PUBLISHED_GUIDANCE
    ),
    QuarantineReason.ambiguous_colliders: _ReasonText(
        "more than one local file claims that number", _LOCAL_GUIDANCE
    ),
    QuarantineReason.unparseable_collider: _ReasonText(
        "the local file does not parse as a decision", _LOCAL_GUIDANCE
    ),
    QuarantineReason.store_unverifiable: _ReasonText(
        "another store file could not be read, so references to that number cannot be verified",
        _LOCAL_GUIDANCE,
    ),
    QuarantineReason.referenced: _ReasonText(
        "the local file takes part in a supersession", _LOCAL_GUIDANCE
    ),
    QuarantineReason.question_reference: _ReasonText(
        "an open question records that number as its resolution", _LOCAL_GUIDANCE
    ),
    QuarantineReason.heading_not_rewritable: _ReasonText(
        "the local file's heading is not in the canonical form this tool can renumber",
        _LOCAL_GUIDANCE,
    ),
    QuarantineReason.irregular_entry: _ReasonText(
        "that number is claimed by an entry this tool never reads - either not a "
        "regular file, or not a name decision readers recognise",
        _LOCAL_GUIDANCE,
    ),
    QuarantineReason.non_canonical_path: _ReasonText(
        "its remote path is not spelled 'decisions/<name>.md', the one spelling "
        "this store reads and writes",
        "Nothing local was changed. Re-upload the decision under the canonical "
        "path from the machine that published it.",
    ),
    QuarantineReason.case_variant_present: _ReasonText(
        "a local file differs from it only by letter case, which some "
        "filesystems treat as the same file",
        _LOCAL_GUIDANCE,
    ),
    QuarantineReason.destination_occupied: _ReasonText(
        "the name it would be renamed to is already taken locally", _LOCAL_GUIDANCE
    ),
}


class RenumberRefusedError(Exception):
    """A local renumber was refused before anything on disk was touched.

    Each subclass carries the quarantine reason the caller reports, so a
    refusal can never be attributed to the wrong cause.
    """

    reason: QuarantineReason


class HeadingRewriteError(RenumberRefusedError):
    """The heading is not in the form the rewriter can address."""

    reason = QuarantineReason.heading_not_rewritable


class DestinationOccupiedError(RenumberRefusedError):
    """The name the local file would move to is already taken."""

    reason = QuarantineReason.destination_occupied


@dataclass(frozen=True)
class DecisionVerdict:
    """One classification of one remote decision file."""

    outcome: DecisionOutcome
    colliders: tuple[Path, ...] = ()
    reason: QuarantineReason | None = None

    @property
    def collider(self) -> Path:
        """The single local file this verdict acts on."""
        return self.colliders[0]

    def quarantined_as(self, reason: QuarantineReason) -> DecisionVerdict:
        """This verdict's colliders, re-cast as a quarantine."""
        return DecisionVerdict(DecisionOutcome.quarantine, self.colliders, reason)

    def text(self) -> _ReasonText:
        assert self.reason is not None  # only quarantine verdicts carry a reason
        return _REASON_TEXT[self.reason]


@dataclass(frozen=True)
class RenumberResult:
    """The completed local renumber behind one installed remote decision."""

    old_path: Path
    new_path: Path
    old_num: int
    new_num: int


@dataclass(frozen=True)
class HeadingRepair:
    """One decision file whose heading was realigned with its filename."""

    path: Path
    from_num: int
    to_num: int


class _CompletionRefusal(Enum):
    """Why the completion pass left a mismatched heading alone."""

    store_state = "store-state"
    heading_not_rewritable = "heading-not-rewritable"


@dataclass(frozen=True)
class CompletionOutcome:
    """What the idempotent completion pass found and healed.

    ``unrepairable`` and ``quarantined`` are both report-only, and differ in what a
    human can do: an unrepairable file needs the store graph untangled first, while a
    quarantined one only needs its heading or its name put right.
    """

    repaired: tuple[HeadingRepair, ...] = ()
    unrepairable: tuple[Path, ...] = ()
    quarantined: tuple[Path, ...] = ()
    retargeted_stems: tuple[str, ...] = ()


# ── Isolation ──────────────────────────────────────────────────────────────


def _isolation_defect(
    corpus: DecisionCorpus, candidate: DecisionFile, numbers: tuple[int, ...]
) -> QuarantineReason | None:
    """Return why ``candidate`` is not isolated, or ``None`` when it is.

    Isolated: parses, joins no supersession, named by no question, fully readable store.
    """
    if candidate.parsed().failed:
        return QuarantineReason.unparseable_collider
    references = corpus.references()
    if not references.verifiable:
        return QuarantineReason.store_unverifiable
    decision = candidate.parsed().decision
    assert decision is not None  # the failed parse returned above
    if decision.supersedes is not None or decision.superseded_by is not None:
        return QuarantineReason.referenced
    if any(num in references.supersession_refs for num in numbers):
        return QuarantineReason.referenced
    if any(num in references.question_refs for num in numbers):
        return QuarantineReason.question_reference
    return None


# ── Hash index ─────────────────────────────────────────────────────────────


class _HashIndexEntry(BaseModel):
    """One entry of the propose_decision content-hash index.

    Unknown keys are kept: the entry is rewritten in place through its raw
    dict, so nothing outside ``decision_id`` is disturbed.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str


def _load_hash_index(store_path: Path) -> dict | None:
    path = store_path / DECISION_HASHES_FILE
    if not path.is_file():
        return None
    try:
        loaded = json.loads(read_text_lenient(path))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _save_hash_index(store_path: Path, index: dict) -> None:
    atomic_write_text(
        store_path / DECISION_HASHES_FILE, json.dumps(index, indent=2) + "\n", newline="\n"
    )


def _entry_stem(value: object) -> str | None:
    try:
        return _HashIndexEntry.model_validate(value).decision_id
    except ValidationError:
        return None


def _retarget_hash_index(store_path: Path, old_stem: str, new_stem: str) -> int:
    """Point every hash-index entry naming ``old_stem`` at ``new_stem``."""
    index = _load_hash_index(store_path)
    if index is None:
        return 0
    changed = 0
    for value in index.values():
        if _entry_stem(value) != old_stem:
            continue
        value["decision_id"] = new_stem
        changed += 1
    if changed:
        _save_hash_index(store_path, index)
    return changed


def _repair_dangling_stems(corpus: DecisionCorpus) -> tuple[str, ...]:
    """Retarget hash-index entries whose decision file was renumbered away.

    Only when exactly one decision file carries the same slug, so a deletion is left alone.
    """
    index = _load_hash_index(corpus.store_path)
    if index is None:
        return ()
    on_disk = {entry.stem for entry in corpus.files}
    by_slug: dict[str, list[str]] = {}
    for entry in corpus.files:
        by_slug.setdefault(strip_number_prefix(entry.stem), []).append(entry.stem)

    retargeted: list[str] = []
    for value in index.values():
        stem = _entry_stem(value)
        if stem is None or stem in on_disk:
            continue
        candidates = by_slug.get(strip_number_prefix(stem), [])
        if len(candidates) != 1:
            continue
        value["decision_id"] = candidates[0]
        retargeted.append(stem)
    if retargeted:
        _save_hash_index(corpus.store_path, index)
    return tuple(retargeted)


# ── Classification ─────────────────────────────────────────────────────────


# The names the store's own writers produce. ``_slugify`` lowercases the
# title and keeps only alphanumerics, joining the rest with hyphens, so a
# minted filename is ``NNN-slug.md`` in lowercase; the extension contributes
# the only dot. Underscores are admitted because a hand-placed file may carry
# one and nothing about it is ambiguous. Alphanumeric is Unicode-aware here for
# the same reason it is in the writer: a title in another script slugifies to
# that script, and refusing it would quarantine a decision Nauro itself wrote.
_CANONICAL_EXTRA_CHARS = frozenset("-._")

# Windows resolves these basenames to devices whatever the extension, so a file
# by that name is not a file. They can only arrive from a server, never from a
# local mint, and are refused rather than handed to open(). The superscript
# digits are not decoration: Windows treats COM\u00b9 and COM\u00b2 as the same
# devices as COM1 and COM2, and they pass an alphanumeric check. This is spelled
# out rather than delegated to ``PureWindowsPath.is_reserved`` because that
# method is deprecated from 3.13 and this package supports 3.10 upward.
_DEVICE_DIGITS = tuple("123456789") + ("\u00b9", "\u00b2", "\u00b3")
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in _DEVICE_DIGITS}
    | {f"LPT{digit}" for digit in _DEVICE_DIGITS}
)


def _is_canonical_filename(filename: str) -> bool:
    """True for a filename the store's writers could have minted."""
    if not filename.endswith(".md"):
        return False
    stem = filename[: -len(".md")]
    if not stem or filename != filename.lower():
        return False
    if not any(char.isalnum() for char in stem):
        return False
    if not all(char.isalnum() or char in _CANONICAL_EXTRA_CHARS for char in stem):
        return False
    return stem.split(".")[0].upper() not in _RESERVED_DEVICE_NAMES


def is_canonical_decision_path(rel: str) -> bool:
    """True only for the exact spelling this store reads and writes.

    A path that merely resolves onto that shape is refused; the filename is an allowlist.
    """
    parts = rel.split("/")
    if len(parts) != 2:
        return False
    directory, filename = parts
    if directory != DECISIONS_DIR or not _is_canonical_filename(filename):
        return False
    return f"{DECISIONS_DIR}/{filename}" == rel


def classify_decision(
    corpus: DecisionCorpus,
    remote_path: str,
    remote_content: bytes,
    state: SyncState,
    manifest_paths: frozenset[str],
) -> DecisionVerdict:
    """Decide what to do with one untracked remote decision file.

    Called immediately before the write it authorizes, against the writer's corpus.
    """
    if not is_canonical_decision_path(remote_path):
        # Routed here rather than dropped, but never written: the corpus scan
        # and the kernel's number allocation both enumerate lowercase
        # ``decisions/*.md`` only, so a file installed under any other spelling
        # would be invisible to the very allocator that must not reuse its
        # number, and state recorded under that spelling would not match the
        # path the push scan reports.
        return DecisionVerdict(DecisionOutcome.quarantine, (), QuarantineReason.non_canonical_path)
    filename = remote_path.split("/", 1)[1]
    if corpus.has_name(filename):
        return _classify_same_path(corpus, filename, remote_content, state, manifest_paths)
    variant = corpus.case_variant(filename)
    if variant is not None:
        # Some filesystems fold case, so writing this name could silently
        # overwrite the local file instead of creating a new one.
        return DecisionVerdict(
            DecisionOutcome.quarantine,
            (corpus.store_path / f"decisions/{variant}",),
            QuarantineReason.case_variant_present,
        )
    return _classify_number(corpus, filename, remote_content, state, manifest_paths)


def _classify_same_path(
    corpus: DecisionCorpus,
    filename: str,
    remote_content: bytes,
    state: SyncState,
    manifest_paths: frozenset[str],
) -> DecisionVerdict:
    """The server holds this exact file and nothing here tracks it.

    Adopting it is safe only once no sibling claims its number.
    """
    local = corpus.store_path / DECISIONS_DIR / filename
    if corpus.is_irregular(filename):
        return DecisionVerdict(
            DecisionOutcome.quarantine, (local,), QuarantineReason.irregular_entry
        )

    entry = corpus.file_named(filename)
    if entry is not None:
        sibling = _sibling_defect(corpus, entry, remote_content, state, manifest_paths)
        if sibling is not None:
            return sibling

    # A file with no parseable number claims no number, so there is nothing to
    # contest: its bytes alone decide.
    local_bytes = _read_bytes(local)
    if local_bytes is None:
        return DecisionVerdict(
            DecisionOutcome.quarantine, (local,), QuarantineReason.store_unverifiable
        )
    if local_bytes == remote_content:
        return DecisionVerdict(DecisionOutcome.adopt, (local,))
    return DecisionVerdict(DecisionOutcome.resolve_conflict, (local,))


def _sibling_defect(
    corpus: DecisionCorpus,
    entry: DecisionFile,
    remote_content: bytes,
    state: SyncState,
    manifest_paths: frozenset[str],
) -> DecisionVerdict | None:
    """Refuse the same-path leg while another local file claims its number."""
    siblings = tuple(
        holder.path for holder in corpus.holders(entry.number) if holder.path != entry.path
    )
    irregular = corpus.irregular_holders(entry.number)
    if irregular:
        return DecisionVerdict(
            DecisionOutcome.quarantine, (entry.path,), QuarantineReason.irregular_entry
        )
    if not siblings:
        return None
    if len(siblings) > 1:
        return DecisionVerdict(
            DecisionOutcome.quarantine, siblings, QuarantineReason.ambiguous_colliders
        )
    sibling = next(holder for holder in corpus.files if holder.path == siblings[0])
    verdict = _classify_collider(
        corpus, sibling, entry.number, remote_content, state, manifest_paths
    )
    if verdict.outcome is DecisionOutcome.renumber_local:
        # The sibling can move out of the way; the caller re-classifies after
        # it does, and the same-path leg then proceeds against a free number.
        return verdict
    if verdict.outcome is DecisionOutcome.canonicalize:
        # The sibling is a second copy of the same record, and the name it
        # would take is the one already holding it.
        return verdict.quarantined_as(QuarantineReason.destination_occupied)
    if verdict.reason is not None:
        return verdict
    return verdict.quarantined_as(QuarantineReason.ambiguous_colliders)


def _classify_number(
    corpus: DecisionCorpus,
    filename: str,
    remote_content: bytes,
    state: SyncState,
    manifest_paths: frozenset[str],
) -> DecisionVerdict:
    """No local file has this name: decide whether its number is free."""
    number = extract_decision_number(filename)
    if number is None:
        return DecisionVerdict(DecisionOutcome.install)

    irregular = corpus.irregular_holders(number)
    if irregular:
        return DecisionVerdict(
            DecisionOutcome.quarantine,
            (corpus.store_path / f"decisions/{irregular[0].name}",),
            QuarantineReason.irregular_entry,
        )

    colliding = corpus.holders(number)
    if not colliding:
        return DecisionVerdict(DecisionOutcome.install)
    if len(colliding) > 1:
        return DecisionVerdict(
            DecisionOutcome.quarantine,
            tuple(entry.path for entry in colliding),
            QuarantineReason.ambiguous_colliders,
        )
    return _classify_collider(corpus, colliding[0], number, remote_content, state, manifest_paths)


def _classify_collider(
    corpus: DecisionCorpus,
    collider: DecisionFile,
    number: int,
    remote_content: bytes,
    state: SyncState,
    manifest_paths: frozenset[str],
) -> DecisionVerdict:
    """Apply the collision matrix to the one local file holding ``number``."""
    colliders = (collider.path,)
    if collider.relative_path in state.files:
        return DecisionVerdict(
            DecisionOutcome.quarantine, colliders, QuarantineReason.tracked_locally
        )
    if collider.relative_path in manifest_paths:
        return DecisionVerdict(
            DecisionOutcome.quarantine, colliders, QuarantineReason.published_remotely
        )

    local_bytes = _read_bytes(collider.path)
    if local_bytes is None:
        return DecisionVerdict(
            DecisionOutcome.quarantine, colliders, QuarantineReason.store_unverifiable
        )
    if local_bytes == remote_content:
        return DecisionVerdict(DecisionOutcome.canonicalize, colliders)

    defect = _isolation_defect(corpus, collider, (number,))
    if defect is not None:
        return DecisionVerdict(DecisionOutcome.quarantine, colliders, defect)
    text = collider.text()
    if text is None or not has_rewritable_heading(text, number):
        return DecisionVerdict(
            DecisionOutcome.quarantine, colliders, QuarantineReason.heading_not_rewritable
        )
    return DecisionVerdict(DecisionOutcome.renumber_local, colliders)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


# ── Mutation ───────────────────────────────────────────────────────────────


def apply_canonicalize(corpus: DecisionCorpus, collider: Path, remote_path: str) -> Path:
    """Rename the byte-identical local collider onto the remote filename.

    The number does not change, so only the stem-keyed hash index follows.
    """
    destination = corpus.store_path / remote_path
    text = read_text_lenient(collider)
    os.rename(collider, destination)
    _retarget_hash_index(corpus.store_path, collider.stem, destination.stem)
    corpus.record_renamed(collider, destination, text)
    return destination


def apply_renumber(
    corpus: DecisionCorpus, collider: Path, number: int, reserved_numbers: frozenset[int]
) -> RenumberResult:
    """Move the local collider to a free number so the remote one can install.
    ``reserved_numbers`` are the numbers the server manifest holds, so the new one is
    minted above them. The heading rewrite is verified first: a refusal mutated nothing.
    """
    new_num = max(corpus.claimed_numbers() | reserved_numbers) + 1
    destination = collider.with_name(f"{new_num:03d}-{strip_number_prefix(collider.name)}")
    if corpus.has_name(destination.name):
        raise DestinationOccupiedError(f"{destination.name}: that name is already taken locally")

    current = read_text_lenient(collider)
    rewritten = rewrite_decision_heading(current, number, new_num)
    if heading_number(rewritten) != new_num:
        raise HeadingRewriteError(
            f"{collider.name}: heading could not be rewritten to {new_num:03d}"
        )

    os.rename(collider, destination)
    atomic_write_text(destination, rewritten, newline="\n")

    _retarget_hash_index(corpus.store_path, collider.stem, destination.stem)
    corpus.record_renamed(collider, destination, rewritten)
    return RenumberResult(old_path=collider, new_path=destination, old_num=number, new_num=new_num)


def _complete_heading(
    corpus: DecisionCorpus, entry: DecisionFile, heading: int, *, contested: bool
) -> HeadingRepair | _CompletionRefusal:
    """Realign one half-renamed file's heading, or say why it was left alone."""
    if contested or _isolation_defect(corpus, entry, (entry.number, heading)) is not None:
        return _CompletionRefusal.store_state
    text = entry.text()
    if text is None or not has_rewritable_heading(text, heading):
        return _CompletionRefusal.heading_not_rewritable
    rewritten = rewrite_decision_heading(text, heading, entry.number)
    atomic_write_text(entry.path, rewritten, newline="\n")
    corpus.record_rewritten(entry.path, rewritten)
    return HeadingRepair(path=entry.path, from_num=heading, to_num=entry.number)


def run_completion_pass(corpus: DecisionCorpus) -> CompletionOutcome:
    """Finish any renumber that crashed between its steps.

    Idempotent: anything it declines to repair is reported, never half-rewritten.
    """
    mismatched = [
        (entry, heading)
        for entry in corpus.files
        if (heading := entry.heading_number()) is not None and heading != entry.number
    ]
    repaired: list[HeadingRepair] = []
    refused: dict[_CompletionRefusal, list[Path]] = {reason: [] for reason in _CompletionRefusal}
    if mismatched:
        # Irregular entries count as claims. They are never read, so a
        # directory or symlink sitting on the number is exactly the ambiguity
        # that must stop a repair, not a claim to overlook because it has no
        # readable content.
        holders = Counter(entry.number for entry in corpus.files)
        for entry, heading in mismatched:
            contested = holders[entry.number] > 1 or bool(corpus.irregular_holders(entry.number))
            outcome = _complete_heading(corpus, entry, heading, contested=contested)
            if isinstance(outcome, HeadingRepair):
                repaired.append(outcome)
            else:
                refused[outcome].append(entry.path)

    return CompletionOutcome(
        repaired=tuple(repaired),
        unrepairable=tuple(refused[_CompletionRefusal.store_state]),
        quarantined=tuple(refused[_CompletionRefusal.heading_not_rewritable]),
        retargeted_stems=_repair_dangling_stems(corpus),
    )


__all__ = [
    "CompletionOutcome",
    "DestinationOccupiedError",
    "DecisionOutcome",
    "DecisionVerdict",
    "HeadingRepair",
    "HeadingRewriteError",
    "RenumberRefusedError",
    "QuarantineReason",
    "RenumberResult",
    "apply_canonicalize",
    "apply_renumber",
    "classify_decision",
    "is_canonical_decision_path",
    "run_completion_pass",
]
