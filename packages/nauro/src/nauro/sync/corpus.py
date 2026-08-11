"""One view of ``decisions/`` for the lifetime of one sync run.

Every collision verdict is taken against the store as it stands at that moment,
which naively means rescanning and reparsing the whole corpus once per verdict.
On a store with hundreds of decisions and a handful of collisions that is
hundreds of thousands of parses. This module keeps the guarantee and drops the
cost: the pull scans ``decisions/`` once inside the decision lock, reads each
file only as far as the question being asked requires, and tells the corpus
about every mutation it makes, so the in-memory view stays identical to the disk
without re-reading it.

Three levels of detail, each paid for only when asked:

* the **listing** - filenames and the numbers they claim - comes from one
  ``scandir`` with no file reads, and answers "who claims this number?" and
  "does this exact name exist?";
* a file's **heading number** is read by scanning lines until the H1, so a
  filename/heading mismatch is detected without loading bodies;
* a file's **parse** is done at most once, and the reference index folded from
  those parses is rebuilt (never re-read) after a mutation.

Entries that are not regular files - a directory, a symlink, a broken link -
are never read, followed, or modified. They are recorded as irregular, counted
as claiming their number so nothing is minted or installed on top of them, and
reported. A symlink's target can change outside every lock this layer holds, so
no proof about its content would survive the write it authorizes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from nauro_core import Decision, extract_decision_number, parse_decision
from nauro_core.questions import EntryBlock, OpenQuestionsFile

from nauro.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro.store.reader import read_text_lenient
from nauro.sync.headings import heading_line_number, heading_number

# Reading a store file can fail on content (a malformed decision) or on the
# filesystem (a permission change mid-run). Neither may abort a sync: both mean
# "this record cannot be verified", which the classifier answers with a
# quarantine. TypeError is in the set because the decision model takes some
# frontmatter keys as constructor arguments, so a file carrying a reserved key
# such as ``num:`` raises TypeError rather than ValueError.
PARSE_FAILURES = (OSError, TypeError, ValueError)


@dataclass(frozen=True)
class ParsedDecision:
    """One decision file's parse, or the record that it has none."""

    decision: Decision | None
    refs: frozenset[int] = frozenset()

    @property
    def failed(self) -> bool:
        return self.decision is None


_UNPARSEABLE = ParsedDecision(decision=None)


@dataclass
class DecisionFile:
    """One regular decision file, with the reads it has needed so far."""

    path: Path
    number: int
    _text: str | None = None
    _heading: int | None = None
    _heading_known: bool = False
    _parsed: ParsedDecision | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def relative_path(self) -> str:
        return f"{DECISIONS_DIR}/{self.path.name}"

    def text(self) -> str | None:
        """The file's content, read once. None when it cannot be read."""
        if self._text is None:
            try:
                self._text = read_text_lenient(self.path)
            except OSError:
                return None
        return self._text

    def heading_number(self) -> int | None:
        """The number this file's H1 claims, scanning no further than the H1."""
        if self._heading_known:
            return self._heading
        self._heading_known = True
        if self._text is not None:
            self._heading = heading_number(self._text)
            return self._heading
        try:
            with self.path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    found = heading_line_number(line.rstrip("\n"))
                    if found is not None:
                        self._heading = found
                        break
        except OSError:
            self._heading = None
        return self._heading

    def parsed(self) -> ParsedDecision:
        """This file's parse, done at most once per view of the file."""
        if self._parsed is None:
            self._parsed = _parse(self.path, self.text())
        return self._parsed


def _parse(path: Path, text: str | None) -> ParsedDecision:
    if text is None:
        return _UNPARSEABLE
    try:
        decision = parse_decision(text, path.name)
    except PARSE_FAILURES:
        return _UNPARSEABLE
    refs = {int(ref) for ref in (decision.supersedes, decision.superseded_by) if ref is not None}
    return ParsedDecision(decision=decision, refs=frozenset(refs))


@dataclass(frozen=True)
class ReferenceIndex:
    """Every place a decision number is named, folded from the parsed corpus."""

    unparseable: frozenset[Path]
    supersession_refs: frozenset[int]
    question_refs: frozenset[int]
    questions_readable: bool

    @property
    def verifiable(self) -> bool:
        """True when every record that could name a number could be read."""
        return not self.unparseable and self.questions_readable


class SkipReason(str, Enum):
    """Why an entry under ``decisions/`` is listed but never read."""

    not_a_regular_file = "not-a-regular-file"
    unreadable_name = "unreadable-name"


@dataclass(frozen=True)
class IrregularEntry:
    """An entry under ``decisions/`` this layer will not read or modify.

    Either it is not a regular file, or its name is not one the store's readers
    recognise - ``list_decisions`` and the kernel's allocator match a lowercase
    ``.md`` suffix literally, so a file named ``007-x.MD`` is invisible to them
    however readable it is here. Both shapes still claim their number, because a
    number claimed by something nothing can read is exactly the ambiguity that
    must hold a remote decision back rather than let it land alongside.
    """

    name: str
    number: int | None
    reason: SkipReason


@dataclass
class DecisionCorpus:
    """The decision store as one sync run sees it, kept current by its writer."""

    store_path: Path
    _files: dict[str, DecisionFile] = field(default_factory=dict)
    _irregular: tuple[IrregularEntry, ...] = ()
    _irregular_names: frozenset[str] = frozenset()
    _names: frozenset[str] = frozenset()
    _references: ReferenceIndex | None = None
    _questions: tuple[frozenset[int], bool] | None = None

    @classmethod
    def scan(cls, store_path: Path) -> DecisionCorpus:
        """List ``decisions/`` without reading a single file."""
        corpus = cls(store_path=store_path)
        directory = store_path / DECISIONS_DIR
        names: set[str] = set()
        irregular: list[IrregularEntry] = []
        try:
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            return corpus
        for entry in entries:
            # Case-folded, so a file the store's own readers cannot see is at
            # least visible here: it still occupies its number and its name is
            # still a case variant of something the server may send.
            if not entry.name.casefold().endswith(".md"):
                continue
            names.add(entry.name)
            number = extract_decision_number(entry.name)
            # follow_symlinks=False: a symlink is not a file this layer reads,
            # follows, or rewrites, whatever it points at.
            if not entry.is_file(follow_symlinks=False):
                irregular.append(
                    IrregularEntry(
                        name=entry.name,
                        number=number,
                        reason=SkipReason.not_a_regular_file,
                    )
                )
                continue
            if not entry.name.endswith(".md"):
                irregular.append(
                    IrregularEntry(
                        name=entry.name, number=number, reason=SkipReason.unreadable_name
                    )
                )
                continue
            if number is None:
                continue
            corpus._files[entry.name] = DecisionFile(path=Path(entry.path), number=number)
        corpus._names = frozenset(names)
        corpus._irregular = tuple(irregular)
        corpus._irregular_names = frozenset(item.name for item in irregular)
        return corpus

    # ── Listing ────────────────────────────────────────────────────────────

    @property
    def files(self) -> tuple[DecisionFile, ...]:
        return tuple(self._files.values())

    @property
    def irregular(self) -> tuple[IrregularEntry, ...]:
        return self._irregular

    def holders(self, number: int) -> tuple[DecisionFile, ...]:
        """Regular decision files claiming ``number``."""
        return tuple(entry for entry in self._files.values() if entry.number == number)

    def irregular_holders(self, number: int) -> tuple[IrregularEntry, ...]:
        return tuple(entry for entry in self._irregular if entry.number == number)

    def claimed_numbers(self) -> frozenset[int]:
        """Every number claimed on disk, including by entries never read."""
        return frozenset(
            {entry.number for entry in self._files.values()}
            | {entry.number for entry in self._irregular if entry.number is not None}
        )

    def file_named(self, name: str) -> DecisionFile | None:
        """The numbered regular file under this exact name, if there is one."""
        return self._files.get(name)

    def is_irregular(self, name: str) -> bool:
        """True when this listed name is not a regular file."""
        return name in self._irregular_names

    def has_name(self, name: str) -> bool:
        """True when ``decisions/`` holds this exact filename, case included."""
        return name in self._names

    def case_variant(self, name: str) -> str | None:
        """A listed name differing from ``name`` only by case, if there is one."""
        if name in self._names:
            return None
        folded = name.lower()
        for listed in sorted(self._names):
            if listed.lower() == folded:
                return listed
        return None

    # ── Parsed view ────────────────────────────────────────────────────────

    def references(self) -> ReferenceIndex:
        """Every supersession and question reference the store records.

        Folded from per-file parses, so a rebuild after a mutation reparses
        only the files that mutation created or changed.
        """
        if self._references is None:
            unparseable: set[Path] = set()
            refs: set[int] = set()
            for entry in self._files.values():
                parsed = entry.parsed()
                if parsed.failed:
                    unparseable.add(entry.path)
                    continue
                refs |= parsed.refs
            question_refs, readable = self._question_references()
            self._references = ReferenceIndex(
                unparseable=frozenset(unparseable),
                supersession_refs=frozenset(refs),
                question_refs=question_refs,
                questions_readable=readable,
            )
        return self._references

    def _question_references(self) -> tuple[frozenset[int], bool]:
        if self._questions is None:
            self._questions = _read_question_references(self.store_path)
        return self._questions

    # ── Mutation notices ───────────────────────────────────────────────────

    def record_added(self, path: Path, text: str) -> None:
        """Note a decision file this run created, with the bytes it wrote."""
        self._names = self._names | {path.name}
        self._replace(path, text)

    def record_renamed(self, old_path: Path, new_path: Path, text: str) -> None:
        """Note a decision file this run moved, with the bytes now at the new path."""
        self._files.pop(old_path.name, None)
        self._names = (self._names - {old_path.name}) | {new_path.name}
        self._replace(new_path, text)

    def record_rewritten(self, path: Path, text: str) -> None:
        """Note new bytes this run wrote to a decision file already listed."""
        self._replace(path, text)

    def _replace(self, path: Path, text: str) -> None:
        number = extract_decision_number(path.name)
        if number is not None:
            self._files[path.name] = DecisionFile(path=path, number=number, _text=text)
        self._references = None


def _read_question_references(store_path: Path) -> tuple[frozenset[int], bool]:
    """Decision numbers named as question resolutions, and whether that is known."""
    path = store_path / OPEN_QUESTIONS_MD
    if not path.is_file():
        return frozenset(), True
    try:
        parsed = OpenQuestionsFile.parse(read_text_lenient(path))
    except PARSE_FAILURES:
        return frozenset(), False
    return (
        frozenset(
            block.entry.resolved_by.decision_num
            for block in parsed.blocks
            if isinstance(block, EntryBlock) and block.entry.resolved_by is not None
        ),
        True,
    )


__all__ = [
    "PARSE_FAILURES",
    "SkipReason",
    "DecisionCorpus",
    "DecisionFile",
    "IrregularEntry",
    "ParsedDecision",
    "ReferenceIndex",
]
