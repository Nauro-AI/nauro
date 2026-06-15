"""Pydantic model for open-questions.md.

The authoritative shape for parsed open-questions.md content. Mirrors
``decision_model.Decision``: ``OpenQuestionsFile.parse`` reads markdown,
``format`` writes it back, and validation lives on the model.

A question entry is identified by its ``[Q###]`` prefix (sequential int
minted by the writer as ``max(num) + 1`` against the existing store).
The parser also accepts the legacy ``[YYYY-MM-DD HH:MM UTC]`` form so
entries written before the Q-form rollout keep round-tripping without
rewrite. The discriminator is which of ``num`` / ``timestamp`` is set on
:class:`QuestionEntry`. Resolution sets ``resolved_by`` on the matching
:class:`EntryBlock`; ``format`` then re-emits it with the ``[Resolved by
Dnn on YYYY-MM-DD]`` prefix. The ``## Resolved`` subsection is excluded
from L0 reads via the divider index on the parsed model.

The internal shape is a flat ``blocks`` list. Each markdown line maps to
exactly one block (``HeaderBlock``, ``ProseBlock``, ``EntryBlock``,
``TripleHashBlock``, ``UnparsableBlock``), so ``parse → format`` is
byte-identical for any input that doesn't pass through ``resolve``. The
``## Resolved`` divider is positional: its block index splits the list
into open-section and resolved-section regions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M UTC"
_RESOLVED_HEADER = "## Resolved"
_DEFAULT_HEADER = "# Open Questions"
_RESOLVED_PREFIX_TOKEN = "Resolved by D"
_RESOLVED_PREFIX_SEP = " on "


class ResolvedRef(BaseModel):
    """Pointer to the decision that resolved a question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_num: int = Field(ge=1)
    date: _date


class QuestionEntry(BaseModel):
    """A single question entry.

    Open when ``resolved_by`` is None; resolved otherwise. Exactly one of
    ``num`` (Q-form, the canonical id) or ``timestamp`` (legacy form,
    accepted on parse so older entries round-trip without rewrite) must
    be set; the validator below enforces this.
    """

    model_config = ConfigDict(extra="forbid")

    num: int | None = Field(default=None, ge=1)
    timestamp: datetime | None = None
    body: str
    continuation: list[str] = Field(default_factory=list)
    resolved_by: ResolvedRef | None = None

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
            return f"Q{self.num}"
        assert self.timestamp is not None  # exactly_one_id validator
        return self.timestamp.strftime(_TIMESTAMP_FMT)

    def render(self) -> list[str]:
        """Return the markdown lines for this entry."""
        if self.resolved_by is not None:
            ref = self.resolved_by
            head = (
                f"- [Resolved by D{ref.decision_num} on {ref.date.isoformat()}] "
                f"[{self.id}] {self.body}"
            )
        else:
            head = f"- [{self.id}] {self.body}"
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

    Attributes:
        old_id: The legacy ``YYYY-MM-DD HH:MM UTC`` id the entry carried.
        new_id: The minted ``Q###`` id that replaces it.
        logged: The ``(logged YYYY-MM-DD HH:MM UTC)`` text appended to the
            body so the human timestamp survives the id rewrite.
    """

    old_id: str
    new_id: str
    logged: str


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of :meth:`OpenQuestionsFile.migrate`.

    Attributes:
        file: A new :class:`OpenQuestionsFile` with legacy entries minted
            into Q-form. Non-legacy blocks are the same objects as the
            input, so a file with no legacy entries returns an unchanged
            ``blocks`` list (idempotent).
        renames: One :class:`MigrationRename` per legacy entry migrated, in
            block order. Empty when nothing was migrated.
    """

    file: OpenQuestionsFile
    renames: tuple[MigrationRename, ...]


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of :meth:`OpenQuestionsFile.resolve`.

    Attributes:
        file: A new :class:`OpenQuestionsFile` with the resolutions applied.
        moved_ids: Ids whose state ended in ``resolved_by`` set. Includes
            ids that were already resolved (idempotent — desired state
            achieved).
        unknown_ids: Ids absent from the file. The caller decides whether
            to surface this as a hard rejection.
        ambiguous_ids: Ids that matched more than one EntryBlock. When
            non-empty no blocks were mutated — the input file is returned
            unchanged. Defensive guard for callers that bypassed the
            pipeline's ambiguity gate.
    """

    file: OpenQuestionsFile
    moved_ids: tuple[str, ...]
    unknown_ids: tuple[str, ...]
    ambiguous_ids: tuple[str, ...] = ()


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
        """Entries whose resolution annotation is unset, in file order.

        Annotation-authoritative, not positional: an entry is unresolved iff
        its ``resolved_by`` is None, regardless of whether it sits before or
        after the ``## Resolved`` divider. This is the definition a reader of
        genuinely-still-open questions wants. It differs from :attr:`open_ids`,
        which partitions strictly on divider position and is kept for the
        round-trip and resolve machinery that depends on physical layout.
        """
        return [
            b.entry
            for b in self.blocks
            if isinstance(b, EntryBlock) and b.entry.resolved_by is None
        ]

    @property
    def known_question_ids(self) -> set[str]:
        """All ids the file knows about: entry ids plus triple-hash embedded ids."""
        known: set[str] = set()
        for b in self.blocks:
            if isinstance(b, EntryBlock):
                known.add(b.entry.id)
            elif isinstance(b, TripleHashBlock) and b.embedded_id is not None:
                known.add(b.embedded_id)
        return known

    @property
    def ambiguous_ids(self) -> dict[str, int]:
        """EntryBlock ids that appear more than once in the file (id -> count).

        Legacy timestamp ids can collide when two questions were logged
        in the same minute. Q-form ids should be unique by construction,
        but the property reports any collision so the boundary can reject
        before mutation.
        """
        counts: dict[str, int] = {}
        for b in self.blocks:
            if isinstance(b, EntryBlock):
                counts[b.entry.id] = counts.get(b.entry.id, 0) + 1
        return {k: v for k, v in counts.items() if v > 1}

    def resolve(
        self,
        ids: list[str],
        decision_num: int,
        date: _date,
    ) -> ResolveResult:
        """Mark entries with the given ids as resolved by ``decision_num``.

        Blocks are flipped in place — they are never reordered. An entry
        physically under ``## Resolved`` stays where it is. When no
        ``## Resolved`` divider exists yet and at least one pre-divider
        entry was flipped, a divider is appended after the existing blocks.

        If any requested id matches more than one EntryBlock, no blocks
        are mutated and ``ResolveResult.ambiguous_ids`` reports the
        offending ids. Defense in depth — the validation pipeline should
        reject ambiguous ids before this call.
        """
        if not ids:
            return ResolveResult(file=self, moved_ids=(), unknown_ids=())

        requested = list(dict.fromkeys(ids))
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
        """Mint a ``Q###`` id for every legacy ``[timestamp]`` entry.

        A legacy entry is an :class:`EntryBlock` whose ``QuestionEntry`` has
        ``timestamp`` set and ``num`` unset. Each such entry is reassigned
        the next sequential id — ``max(num across the whole file) + 1``,
        continuing past any existing Q ids — with ``timestamp`` cleared and
        the original timestamp appended to the body as
        ``(logged YYYY-MM-DD HH:MM UTC)``.

        A resolved legacy entry keeps its ``resolved_by`` prefix; only the
        ``[<timestamp>]`` id segment becomes ``[Q###]`` because
        :meth:`QuestionEntry.render` builds the head from model fields.
        Entries are never reordered — an entry physically under
        ``## Resolved`` stays there with its id rewritten.

        Only touched legacy entries are ``model_copy``'d; every other block
        is reused by reference, so a file that is already all-Q-form returns
        an unchanged ``blocks`` list and empty ``renames`` (idempotent).
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
                    MigrationRename(old_id=old_id, new_id=f"Q{next_num}", logged=logged)
                )
                next_num += 1
            else:
                new_blocks.append(b)

        return MigrationResult(
            file=self.model_copy(update={"blocks": new_blocks}),
            renames=tuple(renames),
        )


def _parse_header(lines: list[str], i: int) -> tuple[str, int]:
    """Skip leading blanks and capture the file-level ``# `` header if present.

    Returns ``(header, next_i)``. The ``startswith("# ")`` / not-``startswith("## ")``
    guard distinguishes the file header from section markers like ``## Resolved``.
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

    Returns ``(entry, lines_consumed)``. ``entry`` is None when the line
    can't be parsed; the caller is responsible for preserving the original
    line (typically as an :class:`UnparsableBlock`).
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
            timestamp = datetime.strptime(id_str, _TIMESTAMP_FMT)
        except ValueError:
            return None, 1

    continuation: list[str] = []
    j = start + 1
    while j < len(lines) and lines[j].startswith("  "):
        continuation.append(lines[j])
        j += 1

    return (
        QuestionEntry(
            num=num,
            timestamp=timestamp,
            body=body,
            continuation=continuation,
            resolved_by=resolved_ref,
        ),
        j - start,
    )


def _parse_q_id(text: str) -> int | None:
    """Parse a ``Q\\d+`` id string into the integer num, or None.

    Plain string ops — file style avoids regex per ``_extract_embedded_id``.
    Mirrors :class:`QuestionEntry`'s ``num`` ``ge=1`` constraint at the
    parse layer so a literal ``[Q0]`` line degrades to ``UnparsableBlock``
    via the strptime fallback rather than raising ``ValidationError`` out
    of ``_parse_entry``.
    """
    if len(text) < 2 or text[0] != "Q":
        return None
    digits = text[1:]
    if not digits.isdigit():
        return None
    num = int(digits)
    if num < 1:
        return None
    return num


def _parse_resolved_prefix(text: str) -> ResolvedRef | None:
    """Parse ``Resolved by Dnn on YYYY-MM-DD`` into a :class:`ResolvedRef`."""
    if not text.startswith(_RESOLVED_PREFIX_TOKEN):
        return None
    rest = text[len(_RESOLVED_PREFIX_TOKEN) :]
    sep_idx = rest.find(_RESOLVED_PREFIX_SEP)
    if sep_idx < 1:
        return None
    num_str = rest[:sep_idx]
    date_str = rest[sep_idx + len(_RESOLVED_PREFIX_SEP) :].strip()
    try:
        return ResolvedRef(decision_num=int(num_str), date=_date.fromisoformat(date_str))
    except (ValueError, TypeError):
        return None


def _extract_embedded_id(line: str) -> str | None:
    """Find a ``[Q###]`` or ``[YYYY-MM-DD HH:MM UTC]`` substring in ``line``.

    Returns the inner id text if either grammar parses, else None. Plain
    string ops (no regex).
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
            datetime.strptime(candidate, _TIMESTAMP_FMT)
            return candidate
        except ValueError:
            start = line.find("[", end + 1)
    return None
