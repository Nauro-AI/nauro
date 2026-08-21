"""Pydantic model for open-questions.md.

The authoritative shape for parsed open-questions.md content, mirroring
``decision_model.Decision``: ``OpenQuestionsFile.parse`` reads markdown,
``format`` writes it back, and validation lives on the model.

An entry is identified by its ``[Q###]`` prefix, minted by the writer as
``max(num) + 1``. The parser also accepts the legacy ``[YYYY-MM-DD HH:MM UTC]``
form, so pre-rollout entries round-trip without rewrite; the discriminator is
which of ``num`` / ``timestamp`` is set. Resolution sets ``resolved_by`` on the
matching ``EntryBlock``; ``format`` re-emits it with a ``[Resolved by Dnn]`` prefix.

The internal shape is a flat ``blocks`` list, one block per markdown line, so
``parse -> format`` is byte-identical unless the file passes through ``resolve``.
The ``## Resolved`` divider is positional: its block index splits the regions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from nauro_core.constants import POINTER_FLAG_PREFIXES, QUESTION_ENTRY_CHAR_BUDGET
from nauro_core.parsing import is_ascii_decimal

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M UTC"
_RESOLVED_HEADER = "## Resolved"
_DEFAULT_HEADER = "# Open Questions"
_RESOLVED_PREFIX_TOKEN = "Resolved by D"
_RESOLVED_PREFIX_SEP = " on "

# Appended to over-budget entry text by :func:`truncate_entry_text` so a
# capped payload never silently hides content.
QUESTION_TRUNCATION_POINTER = '... [truncated - full text: get_raw_file("open-questions.md")]'


class InvalidQuestionIdentifier(ValueError):
    """A question identifier is not valid for the requested boundary."""

    def __init__(self, field: str, requirement: str) -> None:
        self.field = field
        super().__init__(f"{field} must be {requirement}.")


class InvalidLegacyQuestionIdentifier(ValueError):
    """A legacy question identifier is not in its permanent exact grammar."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{field} must use valid YYYY-MM-DD HH:MM UTC form.")


def format_question_id(number: object, *, field: str = "question_number") -> str:
    """Return the canonical unpadded Q-form for a positive integer."""
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise InvalidQuestionIdentifier(field, "a positive integer")
    return "Q" + _format_positive_ascii_decimal(number)


def parse_question_id(value: object, *, field: str = "question_id") -> int:
    """Return the numeric identity of a compatible Q-form input."""
    if not isinstance(value, str) or len(value) < 2 or value[0] != "Q":
        raise InvalidQuestionIdentifier(field, "Q followed by positive ASCII decimal digits")
    digits = value[1:]
    if not is_ascii_decimal(digits):
        raise InvalidQuestionIdentifier(field, "Q followed by positive ASCII decimal digits")
    number = 0
    for digit in digits:
        number = number * 10 + ord(digit) - ord("0")
    if number < 1:
        raise InvalidQuestionIdentifier(field, "Q followed by positive ASCII decimal digits")
    return number


def _format_positive_ascii_decimal(number: int) -> str:
    chunks: list[int] = []
    while number:
        number, remainder = divmod(number, 1_000_000_000)
        chunks.append(remainder)
    head = str(chunks.pop())
    return head + "".join(f"{chunk:09d}" for chunk in reversed(chunks))


def validate_question_id(value: object, *, field: str = "question_id") -> str:
    """Return a canonical unpadded Q-form or raise."""
    number = parse_question_id(value, field=field)
    canonical = format_question_id(number, field=field)
    if value != canonical:
        raise InvalidQuestionIdentifier(field, "a canonical unpadded Q-form")
    return canonical


def validate_legacy_question_id(
    value: object,
    *,
    field: str = "legacy_question_id",
) -> str:
    """Return an exact calendar-valid legacy timestamp identifier or raise."""
    if not isinstance(value, str) or len(value) != 20:
        raise InvalidLegacyQuestionIdentifier(field)
    if value[4] != "-" or value[7] != "-" or value[10] != " ":
        raise InvalidLegacyQuestionIdentifier(field)
    if value[13] != ":" or value[16:] != " UTC":
        raise InvalidLegacyQuestionIdentifier(field)
    digits = (value[0:4], value[5:7], value[8:10], value[11:13], value[14:16])
    if not all(is_ascii_decimal(part) for part in digits):
        raise InvalidLegacyQuestionIdentifier(field)
    try:
        parsed = datetime.strptime(value, _TIMESTAMP_FMT)
    except ValueError as exc:
        raise InvalidLegacyQuestionIdentifier(field) from exc
    if parsed.strftime(_TIMESTAMP_FMT) != value:
        raise InvalidLegacyQuestionIdentifier(field)
    return value


def truncate_entry_text(text: str) -> str:
    """Cap rendered question-entry text at :data:`QUESTION_ENTRY_CHAR_BUDGET`.

    Text within budget is byte-unchanged; a cut ends with ``QUESTION_TRUNCATION_POINTER``.
    """
    if len(text) <= QUESTION_ENTRY_CHAR_BUDGET:
        return text
    return text[:QUESTION_ENTRY_CHAR_BUDGET].rstrip() + " " + QUESTION_TRUNCATION_POINTER


class ResolvedRef(BaseModel):
    """Pointer to the decision that resolved a question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_num: int = Field(ge=1)
    date: _date


class QuestionEntry(BaseModel):
    """A single question entry.

    Open when ``resolved_by`` is None, resolved otherwise. Exactly one of ``num``
    (Q-form, canonical) or ``timestamp`` (legacy, accepted on parse so older
    entries round-trip) is set; the validator enforces it.
    """

    model_config = ConfigDict(extra="forbid")

    num: int | None = Field(default=None, ge=1)
    timestamp: datetime | None = None
    body: str
    continuation: list[str] = Field(default_factory=list)
    resolved_by: ResolvedRef | None = None
    _source_question_id: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def exactly_one_id(self) -> QuestionEntry:
        if (self.num is None) == (self.timestamp is None):
            raise ValueError(
                "QuestionEntry requires exactly one of num or timestamp to be set; "
                f"got num={self.num!r}, timestamp={self.timestamp!r}."
            )
        return self

    @property
    def id(self) -> str:
        if self.num is not None:
            return format_question_id(self.num)
        assert self.timestamp is not None  # exactly_one_id validator
        return self.timestamp.strftime(_TIMESTAMP_FMT)

    @property
    def _render_id(self) -> str:
        return self._source_question_id or self.id

    @property
    def is_discovery_pointer(self) -> bool:
        """True when the body is a discovery pointer, not a question for review.

        Checked on the left-stripped body against :data:`POINTER_FLAG_PREFIXES`.
        """
        return self.body.lstrip().startswith(POINTER_FLAG_PREFIXES)

    def render(self) -> list[str]:
        """Return the markdown lines for this entry."""
        if self.resolved_by is not None:
            ref = self.resolved_by
            head = (
                f"- [Resolved by D{ref.decision_num} on {ref.date.isoformat()}] "
                f"[{self._render_id}] {self.body}"
            )
        else:
            head = f"- [{self._render_id}] {self.body}"
        return [head, *self.continuation]


class HeaderBlock(BaseModel):
    """A ``##`` section header line. ``is_resolved_divider`` marks the
    ``## Resolved`` boundary that partitions the file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["header"] = "header"
    text: str
    is_resolved_divider: bool

    def render_lines(self) -> list[str]:
        return [self.text]


class ProseBlock(BaseModel):
    """A run of non-entry lines (free-form prose and blanks). Verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["prose"] = "prose"
    lines: tuple[str, ...]

    def render_lines(self) -> list[str]:
        return list(self.lines)


class EntryBlock(BaseModel):
    """A parsed ``- [...]`` question entry plus its continuation lines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["entry"] = "entry"
    entry: QuestionEntry

    def render_lines(self) -> list[str]:
        return self.entry.render()


class TripleHashBlock(BaseModel):
    """A ``### `` topic header plus its directly-following body lines.

    ``embedded_id`` is set when the head line contains a parseable
    ``[YYYY-MM-DD HH:MM UTC]`` substring (treated as a question id for
    boundary validation), otherwise None.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["triple_hash"] = "triple_hash"
    lines: tuple[str, ...]
    embedded_id: str | None

    def render_lines(self) -> list[str]:
        return list(self.lines)


class UnparsableBlock(BaseModel):
    """A line that started with ``- [`` but couldn't be parsed as an entry.

    Preserved verbatim so that round-tripping never silently drops user content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unparsable"] = "unparsable"
    lines: tuple[str, ...]

    def render_lines(self) -> list[str]:
        return list(self.lines)


Block = Annotated[
    HeaderBlock | ProseBlock | EntryBlock | TripleHashBlock | UnparsableBlock,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class MigrationRename:
    """One legacy entry's rename, recorded by :meth:`OpenQuestionsFile.migrate`.

    ``old_id`` is the legacy ``YYYY-MM-DD HH:MM UTC`` id, ``new_id`` the minted
    ``Q###`` that replaces it, and ``logged`` the ``(logged ...)`` text appended to
    the body so the human timestamp survives the rewrite.
    """

    old_id: str
    new_id: str
    logged: str


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of :meth:`OpenQuestionsFile.migrate`.

    ``file`` is a new :class:`OpenQuestionsFile` with legacy entries minted into
    Q-form; non-legacy blocks are the same objects, so a file with nothing to
    migrate returns unchanged. ``renames`` holds one rename per entry, block order.
    """

    file: OpenQuestionsFile
    renames: tuple[MigrationRename, ...]


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of :meth:`OpenQuestionsFile.resolve`.
    ``file`` carries the applied resolutions. ``moved_ids`` are the requested ids
    whose ``resolved_by`` ended set, in place (already-resolved ids included, so
    the call is idempotent); ``unknown_ids`` are absent from the file. A non-empty
    ``ambiguous_ids`` means the input file was returned unmutated.
    """

    file: OpenQuestionsFile
    moved_ids: tuple[str, ...]
    unknown_ids: tuple[str, ...]
    ambiguous_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizeResult:
    """Outcome of :meth:`OpenQuestionsFile.normalize`.

    ``file`` relocates prose-safe resolved entries below the ``## Resolved`` divider,
    returning the input unchanged when nothing is eligible. ``relocated_ids`` names
    what moved, in file order; ``skipped_prose_ids`` what trailing prose held back.
    """

    file: OpenQuestionsFile
    relocated_ids: tuple[str, ...]
    skipped_prose_ids: tuple[str, ...]


class OpenQuestionsFile(BaseModel):
    """Parsed ``open-questions.md``. Round-trips through ``parse`` and ``format``."""

    model_config = ConfigDict(extra="forbid")

    header: str = _DEFAULT_HEADER
    blocks: list[Block] = Field(default_factory=list)

    @classmethod
    def parse(cls, content: str) -> OpenQuestionsFile:
        """Parse ``open-questions.md`` content into an :class:`OpenQuestionsFile`."""
        lines = content.split("\n")
        n = len(lines)
        header, i = _parse_header(lines, 0)

        blocks: list[Block] = []
        pending_prose: list[str] = []

        def flush_prose() -> None:
            if pending_prose:
                blocks.append(ProseBlock(lines=tuple(pending_prose)))
                pending_prose.clear()

        while i < n:
            line = lines[i]

            if line.startswith("## "):
                flush_prose()
                stripped = line.strip()
                blocks.append(
                    HeaderBlock(
                        text=line.rstrip(),
                        is_resolved_divider="resolved" in stripped.lower(),
                    )
                )
                i += 1
                continue

            if line.startswith("### "):
                flush_prose()
                head_line = line
                triple_lines: list[str] = [head_line]
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if (
                        not nxt.strip()
                        or nxt.startswith("## ")
                        or nxt.startswith("### ")
                        or nxt.startswith("- [")
                    ):
                        break
                    triple_lines.append(nxt)
                    j += 1
                embedded_id = _extract_embedded_id(head_line)
                blocks.append(
                    TripleHashBlock(
                        lines=tuple(triple_lines),
                        embedded_id=embedded_id,
                    )
                )
                i = j
                continue

            if line.startswith("- ["):
                flush_prose()
                entry, consumed = _parse_entry(lines, i)
                if entry is None:
                    blocks.append(UnparsableBlock(lines=(line,)))
                    i += 1
                else:
                    blocks.append(EntryBlock(entry=entry))
                    i += consumed
                continue

            pending_prose.append(line)
            i += 1

        flush_prose()

        return cls(header=header, blocks=blocks)

    def format(self) -> str:
        """Render the file back to markdown."""
        out: list[str] = [self.header]
        for b in self.blocks:
            out.extend(b.render_lines())
        return "\n".join(out)

    @property
    def resolved_divider_idx(self) -> int | None:
        """Index of the ``## Resolved`` HeaderBlock in :attr:`blocks`, or None."""
        for idx, b in enumerate(self.blocks):
            if isinstance(b, HeaderBlock) and b.is_resolved_divider:
                return idx
        return None

    @property
    def open_ids(self) -> list[str]:
        """Entry ids appearing before the ``## Resolved`` divider (or all if no divider)."""
        divider = self.resolved_divider_idx
        result: list[str] = []
        for idx, b in enumerate(self.blocks):
            if divider is not None and idx >= divider:
                break
            if isinstance(b, EntryBlock):
                result.append(b.entry.id)
        return result

    @property
    def resolved_ids(self) -> list[str]:
        """Entry ids appearing after the ``## Resolved`` divider."""
        divider = self.resolved_divider_idx
        if divider is None:
            return []
        result: list[str] = []
        for b in self.blocks[divider + 1 :]:
            if isinstance(b, EntryBlock):
                result.append(b.entry.id)
        return result

    @property
    def unresolved_entries(self) -> list[QuestionEntry]:
        """Entries whose ``resolved_by`` is None, in file order.
        Annotation-authoritative regardless of divider position, unlike the
        strictly positional :attr:`open_ids` partition.
        """
        return [
            b.entry
            for b in self.blocks
            if isinstance(b, EntryBlock) and b.entry.resolved_by is None
        ]

    @property
    def genuine_open_entries(self) -> list[QuestionEntry]:
        """Unresolved entries that are not discovery pointers, in file order.

        The genuinely-open questions a human should see; ``resolved_by`` is authoritative.
        """
        return [e for e in self.unresolved_entries if not e.is_discovery_pointer]

    @property
    def discovery_pointers(self) -> list[QuestionEntry]:
        """Unresolved entries that are discovery pointers, in file order.

        The complement of :attr:`genuine_open_entries`: breadcrumbs, not questions.
        """
        return [e for e in self.unresolved_entries if e.is_discovery_pointer]

    @property
    def numbered_entries(self) -> list[QuestionEntry]:
        """Entries carrying a ``Q`` number, resolved history included, in file order.

        The allocation domain for the next ``Q###``: a number stays reserved for life.
        """
        return [
            b.entry for b in self.blocks if isinstance(b, EntryBlock) and b.entry.num is not None
        ]

    @property
    def known_question_ids(self) -> set[str]:
        """All ids the file knows about: entry ids plus triple-hash embedded ids."""
        known: set[str] = set()
        for b in self.blocks:
            if isinstance(b, EntryBlock):
                known.add(b.entry.id)
                if b.entry._source_question_id is not None:
                    known.add(b.entry._source_question_id)
            elif isinstance(b, TripleHashBlock) and b.embedded_id is not None:
                known.add(b.embedded_id)
                number = _parse_q_id(b.embedded_id)
                if number is not None:
                    known.add(format_question_id(number))
        return known

    @property
    def ambiguous_ids(self) -> dict[str, int]:
        """EntryBlock ids that appear more than once in the file (id -> count).

        Legacy timestamp ids can collide within a minute, so a boundary can reject first.
        """
        counts: dict[str, int] = {}
        aliases: dict[str, set[str]] = {}
        for b in self.blocks:
            if isinstance(b, EntryBlock):
                canonical_id = b.entry.id
                counts[canonical_id] = counts.get(canonical_id, 0) + 1
                aliases.setdefault(canonical_id, set()).add(canonical_id)
                if b.entry._source_question_id is not None:
                    aliases[canonical_id].add(b.entry._source_question_id)
        ambiguous: dict[str, int] = {}
        for canonical_id, count in counts.items():
            if count > 1:
                ambiguous.update({alias: count for alias in sorted(aliases[canonical_id])})
        return ambiguous

    def resolve(
        self,
        ids: list[str],
        decision_num: int,
        date: _date,
    ) -> ResolveResult:
        """Set ``resolved_by`` on the entries named by ``ids``, in place; blocks are never
        reordered. ``## Resolved`` is appended when no divider is detected (any ``## `` header
        naming "resolved" counts) and a pre-divider entry flipped. Ambiguous ids: no mutation.
        """
        if not ids:
            return ResolveResult(file=self, moved_ids=(), unknown_ids=())

        requested = list(dict.fromkeys(_canonicalize_question_reference(value) for value in ids))
        ambiguous_map = self.ambiguous_ids
        ambiguous_requested = tuple(i for i in requested if i in ambiguous_map)
        if ambiguous_requested:
            return ResolveResult(
                file=self,
                moved_ids=(),
                unknown_ids=(),
                ambiguous_ids=ambiguous_requested,
            )

        ref = ResolvedRef(decision_num=decision_num, date=date)
        requested_set = set(requested)
        entry_ids = {b.entry.id for b in self.blocks if isinstance(b, EntryBlock)}
        moved = tuple(i for i in requested if i in entry_ids)
        unknown = tuple(i for i in requested if i not in entry_ids)

        divider_idx = self.resolved_divider_idx
        new_blocks: list[Block] = []
        any_pre_divider_flip = False
        for idx, b in enumerate(self.blocks):
            if (
                isinstance(b, EntryBlock)
                and b.entry.id in requested_set
                and b.entry.resolved_by is None
            ):
                new_entry = b.entry.model_copy(update={"resolved_by": ref})
                new_blocks.append(EntryBlock(entry=new_entry))
                if divider_idx is None or idx < divider_idx:
                    any_pre_divider_flip = True
            else:
                new_blocks.append(b)

        if divider_idx is None and any_pre_divider_flip:
            new_blocks.append(ProseBlock(lines=("",)))
            new_blocks.append(HeaderBlock(text=_RESOLVED_HEADER, is_resolved_divider=True))

        return ResolveResult(
            file=self.model_copy(update={"blocks": new_blocks}),
            moved_ids=moved,
            unknown_ids=unknown,
        )

    def migrate(self) -> MigrationResult:
        """Mint a ``Q###`` id for every legacy ``[timestamp]`` entry: each takes
        ``max(num across the file) + 1``, clears its timestamp into a ``(logged ...)``
        body suffix, and stays in place. Only touched entries copy, so it is idempotent.
        """
        next_num = (
            max(
                (
                    b.entry.num
                    for b in self.blocks
                    if isinstance(b, EntryBlock) and b.entry.num is not None
                ),
                default=0,
            )
            + 1
        )

        renames: list[MigrationRename] = []
        new_blocks: list[Block] = []
        for b in self.blocks:
            if isinstance(b, EntryBlock) and b.entry.timestamp is not None and b.entry.num is None:
                old_id = b.entry.id
                logged = f"(logged {b.entry.timestamp.strftime(_TIMESTAMP_FMT)})"
                new_body = f"{b.entry.body} {logged}" if b.entry.body else logged
                new_entry = b.entry.model_copy(
                    update={"num": next_num, "timestamp": None, "body": new_body}
                )
                new_blocks.append(EntryBlock(entry=new_entry))
                renames.append(
                    MigrationRename(
                        old_id=old_id,
                        new_id=format_question_id(next_num),
                        logged=logged,
                    )
                )
                next_num += 1
            else:
                new_blocks.append(b)

        return MigrationResult(
            file=self.model_copy(update={"blocks": new_blocks}),
            renames=tuple(renames),
        )

    def normalize(self) -> NormalizeResult:
        """Relocate prose-safe resolved entries below the detected divider (any ``## `` header
        naming "resolved"), minting ``## Resolved`` when none is detected; insertion precedes
        the first header after it. Pure, idempotent, whole-file; prose-trailed entries stay put.
        """
        blocks = self.blocks
        n = len(blocks)
        divider_idx = self.resolved_divider_idx

        relocated_ids: list[str] = []
        skipped_prose_ids: list[str] = []
        to_relocate: list[EntryBlock] = []
        remove_indices: set[int] = set()

        for i, block in enumerate(blocks):
            if not isinstance(block, EntryBlock) or block.entry.resolved_by is None:
                continue
            if divider_idx is not None and i >= divider_idx:
                continue

            trailing_blank: list[int] = []
            prose_safe = True
            j = i + 1
            while j < n:
                nxt = blocks[j]
                if isinstance(nxt, (EntryBlock, HeaderBlock)):
                    break
                if isinstance(nxt, ProseBlock) and all(not line.strip() for line in nxt.lines):
                    trailing_blank.append(j)
                    j += 1
                    continue
                prose_safe = False
                break

            if prose_safe:
                relocated_ids.append(block.entry.id)
                to_relocate.append(block)
                remove_indices.add(i)
                remove_indices.update(trailing_blank)
            else:
                skipped_prose_ids.append(block.entry.id)

        if not to_relocate:
            return NormalizeResult(
                file=self,
                relocated_ids=(),
                skipped_prose_ids=tuple(skipped_prose_ids),
            )

        kept = [b for idx, b in enumerate(blocks) if idx not in remove_indices]

        relocated_blocks: list[Block] = []
        for entry_block in to_relocate:
            relocated_blocks.append(ProseBlock(lines=("",)))
            relocated_blocks.append(entry_block)

        if divider_idx is None:
            new_blocks = [
                *kept,
                ProseBlock(lines=("",)),
                HeaderBlock(text=_RESOLVED_HEADER, is_resolved_divider=True),
                *relocated_blocks,
            ]
        else:
            div_pos = next(
                pos
                for pos, b in enumerate(kept)
                if isinstance(b, HeaderBlock) and b.is_resolved_divider
            )
            insert_pos = len(kept)
            for pos in range(div_pos + 1, len(kept)):
                if isinstance(kept[pos], HeaderBlock):
                    insert_pos = pos
                    break
            new_blocks = [*kept[:insert_pos], *relocated_blocks, *kept[insert_pos:]]

        return NormalizeResult(
            file=self.model_copy(update={"blocks": new_blocks}),
            relocated_ids=tuple(relocated_ids),
            skipped_prose_ids=tuple(skipped_prose_ids),
        )


def _parse_header(lines: list[str], i: int) -> tuple[str, int]:
    """Skip leading blanks and capture the file-level ``# `` header if present.
    Returns ``(header, next_i)``; a ``## `` section marker is never the header.
    """
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    header = _DEFAULT_HEADER
    if i < n and lines[i].startswith("# ") and not lines[i].startswith("## "):
        header = lines[i].rstrip()
        i += 1
    return header, i


def _parse_entry(lines: list[str], start: int) -> tuple[QuestionEntry | None, int]:
    """Parse a single ``- [...] body`` entry starting at ``lines[start]``.

    Returns ``(entry, lines_consumed)``; ``entry`` is None when the line does not parse.
    """
    line = lines[start]
    if not line.startswith("- ["):
        return None, 1

    rest = line[2:]
    first_close = rest.find("]")
    if first_close < 1:
        return None, 1

    first_inside = rest[1:first_close]
    after_first = rest[first_close + 1 :].lstrip()

    resolved_ref: ResolvedRef | None = None
    if first_inside.startswith(_RESOLVED_PREFIX_TOKEN):
        resolved_ref = _parse_resolved_prefix(first_inside)
        if resolved_ref is None or not after_first.startswith("["):
            return None, 1
        second_close = after_first.find("]")
        if second_close < 1:
            return None, 1
        id_str = after_first[1:second_close]
        body = after_first[second_close + 1 :].lstrip()
    else:
        id_str = first_inside
        body = after_first

    num = _parse_q_id(id_str)
    timestamp: datetime | None = None
    if num is None:
        try:
            validate_legacy_question_id(id_str)
            timestamp = datetime.strptime(id_str, _TIMESTAMP_FMT)
        except (InvalidLegacyQuestionIdentifier, ValueError):
            return None, 1

    continuation: list[str] = []
    j = start + 1
    while j < len(lines) and lines[j].startswith("  "):
        continuation.append(lines[j])
        j += 1

    entry = QuestionEntry(
        num=num,
        timestamp=timestamp,
        body=body,
        continuation=continuation,
        resolved_by=resolved_ref,
    )
    if num is not None:
        entry._source_question_id = id_str
    return entry, j - start


def _parse_q_id(text: str) -> int | None:
    """Parse a ``Q<digits>`` id string into the integer num, or None.

    Mirrors ``QuestionEntry``'s ``ge=1`` bound, so ``[Q0]`` degrades to unparsable.
    """
    try:
        return parse_question_id(text)
    except InvalidQuestionIdentifier:
        return None


def _canonicalize_question_reference(value: str) -> str:
    try:
        return format_question_id(parse_question_id(value))
    except InvalidQuestionIdentifier:
        return value


def _parse_resolved_prefix(text: str) -> ResolvedRef | None:
    """Parse ``Resolved by Dnn on YYYY-MM-DD`` into a :class:`ResolvedRef`."""
    if not text.startswith(_RESOLVED_PREFIX_TOKEN):
        return None
    rest = text[len(_RESOLVED_PREFIX_TOKEN) :]
    sep_idx = rest.find(_RESOLVED_PREFIX_SEP)
    if sep_idx < 1:
        return None
    num_str = rest[:sep_idx]
    if not is_ascii_decimal(num_str):
        return None
    date_str = rest[sep_idx + len(_RESOLVED_PREFIX_SEP) :].strip()
    try:
        return ResolvedRef(decision_num=int(num_str), date=_date.fromisoformat(date_str))
    except (ValueError, TypeError):
        return None


def _extract_embedded_id(line: str) -> str | None:
    """Find a ``[Q###]`` or ``[YYYY-MM-DD HH:MM UTC]`` substring in ``line``.

    Returns the inner id text if either grammar parses, else ``None``. No regex.
    """
    start = line.find("[")
    while start != -1:
        end = line.find("]", start + 1)
        if end == -1:
            return None
        candidate = line[start + 1 : end]
        if _parse_q_id(candidate) is not None:
            return candidate
        try:
            validate_legacy_question_id(candidate)
            return candidate
        except InvalidLegacyQuestionIdentifier:
            start = line.find("[", end + 1)
    return None
