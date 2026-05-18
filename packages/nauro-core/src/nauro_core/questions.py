"""Pydantic model for open-questions.md.

The authoritative shape for parsed open-questions.md content. Mirrors
``decision_model.Decision``: ``OpenQuestionsFile.parse`` reads markdown,
``format`` writes it back, and validation lives on the model.

A question entry is identified by the UTC timestamp inside its
``[YYYY-MM-DD HH:MM UTC]`` prefix — the same id ``flag_question`` writes.
Resolution sets ``resolved_by`` on the matching :class:`EntryBlock`;
``format`` then re-emits it with the ``[Resolved by Dnn on
YYYY-MM-DD]`` prefix. ``parse_questions`` (in :mod:`nauro_core.parsing`)
already filters the ``## Resolved`` subsection out of L0 reads.

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

from pydantic import BaseModel, ConfigDict, Field

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

    Open when ``resolved_by`` is None; resolved otherwise. The timestamp
    doubles as the entry's stable id (``id`` property).
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    body: str
    continuation: list[str] = Field(default_factory=list)
    resolved_by: ResolvedRef | None = None

    @property
    def id(self) -> str:
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
class ResolveResult:
    """Outcome of :meth:`OpenQuestionsFile.resolve`.

    Attributes:
        file: A new :class:`OpenQuestionsFile` with the resolutions applied.
        moved_ids: Ids whose state ended in ``resolved_by`` set. Includes
            ids that were already resolved (idempotent — desired state
            achieved).
        unknown_ids: Ids absent from the file. The caller decides whether
            to surface this as a hard rejection.
    """

    file: OpenQuestionsFile
    moved_ids: tuple[str, ...]
    unknown_ids: tuple[str, ...]


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
    def known_question_ids(self) -> set[str]:
        """All ids the file knows about: entry ids plus triple-hash embedded ids."""
        known: set[str] = set()
        for b in self.blocks:
            if isinstance(b, EntryBlock):
                known.add(b.entry.id)
            elif isinstance(b, TripleHashBlock) and b.embedded_id is not None:
                known.add(b.embedded_id)
        return known

    def resolve(
        self,
        ids: list[str],
        decision_num: int,
        date: _date,
    ) -> ResolveResult:
        """Mark entries with the given timestamp ids as resolved by ``decision_num``.

        Blocks are flipped in place — they are never reordered. An entry
        physically under ``## Resolved`` stays where it is. When no
        ``## Resolved`` divider exists yet and at least one pre-divider
        entry was flipped, a divider is appended after the existing blocks.
        """
        if not ids:
            return ResolveResult(file=self, moved_ids=(), unknown_ids=())

        requested = list(dict.fromkeys(ids))
        ref = ResolvedRef(decision_num=decision_num, date=date)
        requested_set = set(requested)
        known = self.known_question_ids
        unknown = tuple(i for i in requested if i not in known)
        moved = tuple(i for i in requested if i in known)

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
        timestamp_str = after_first[1:second_close]
        body = after_first[second_close + 1 :].lstrip()
    else:
        timestamp_str = first_inside
        body = after_first

    try:
        timestamp = datetime.strptime(timestamp_str, _TIMESTAMP_FMT)
    except ValueError:
        return None, 1

    continuation: list[str] = []
    j = start + 1
    while j < len(lines) and lines[j].startswith("  "):
        continuation.append(lines[j])
        j += 1

    return (
        QuestionEntry(
            timestamp=timestamp,
            body=body,
            continuation=continuation,
            resolved_by=resolved_ref,
        ),
        j - start,
    )


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
    """Find a ``[YYYY-MM-DD HH:MM UTC]`` substring in ``line`` and return its id text.

    Uses plain string ops (no regex). Returns the inner timestamp text if
    parseable as the canonical format, else None.
    """
    start = line.find("[")
    while start != -1:
        end = line.find("]", start + 1)
        if end == -1:
            return None
        candidate = line[start + 1 : end]
        try:
            datetime.strptime(candidate, _TIMESTAMP_FMT)
            return candidate
        except ValueError:
            start = line.find("[", end + 1)
    return None
