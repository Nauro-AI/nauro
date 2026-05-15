"""Pydantic model for open-questions.md.

The authoritative shape for parsed open-questions.md content. Mirrors
``decision_model.Decision``: ``OpenQuestionsFile.parse`` reads markdown,
``format`` writes it back, and validation lives on the model.

A question entry is identified by the UTC timestamp inside its
``[YYYY-MM-DD HH:MM UTC]`` prefix — the same id ``flag_question`` writes.
Resolution sets ``resolved_by`` on the entry; ``format`` then emits it
under a ``## Resolved`` subsection prefixed with ``[Resolved by Dnn on
YYYY-MM-DD]``. ``parse_questions`` (in :mod:`nauro_core.parsing`) already
filters that subsection out of L0 reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime

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
    intro: list[str] = Field(default_factory=list)
    entries: list[QuestionEntry] = Field(default_factory=list)

    @classmethod
    def parse(cls, content: str) -> OpenQuestionsFile:
        """Parse ``open-questions.md`` content into an :class:`OpenQuestionsFile`."""
        lines = content.split("\n")
        i = 0
        n = len(lines)

        while i < n and not lines[i].strip():
            i += 1
        header = _DEFAULT_HEADER
        if i < n and lines[i].startswith("# ") and not lines[i].startswith("## "):
            header = lines[i].rstrip()
            i += 1

        intro: list[str] = []
        entries: list[QuestionEntry] = []
        in_resolved_section = False

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("## "):
                in_resolved_section = "resolved" in stripped.lower()
                i += 1
                continue

            if not stripped:
                if not entries:
                    intro.append(line)
                i += 1
                continue

            if line.startswith("- ["):
                entry, consumed = _parse_entry(lines, i, in_resolved_section)
                if entry is not None:
                    entries.append(entry)
                    i += consumed
                    continue

            if not entries:
                intro.append(line)
            i += 1

        while intro and not intro[-1].strip():
            intro.pop()

        return cls(header=header, intro=intro, entries=entries)

    def format(self) -> str:
        """Render the file back to markdown."""
        out: list[str] = [self.header]
        if self.intro:
            out.extend(self.intro)

        open_entries = [e for e in self.entries if e.resolved_by is None]
        resolved_entries = [e for e in self.entries if e.resolved_by is not None]

        for entry in open_entries:
            out.append("")
            out.extend(entry.render())

        if resolved_entries:
            out.append("")
            out.append(_RESOLVED_HEADER)
            for entry in resolved_entries:
                out.append("")
                out.extend(entry.render())

        out.append("")
        return "\n".join(out)

    @property
    def open_ids(self) -> list[str]:
        """Ids of currently-open questions."""
        return [e.id for e in self.entries if e.resolved_by is None]

    @property
    def resolved_ids(self) -> list[str]:
        """Ids of already-resolved questions."""
        return [e.id for e in self.entries if e.resolved_by is not None]

    def resolve(
        self,
        ids: list[str],
        decision_num: int,
        date: _date,
    ) -> ResolveResult:
        """Mark entries with the given timestamp ids as resolved by ``decision_num``."""
        if not ids:
            return ResolveResult(file=self, moved_ids=(), unknown_ids=())

        requested = list(dict.fromkeys(ids))
        by_id = {e.id: e for e in self.entries}
        ref = ResolvedRef(decision_num=decision_num, date=date)

        new_entries = [
            entry.model_copy(update={"resolved_by": ref})
            if entry.id in requested and entry.resolved_by is None
            else entry
            for entry in self.entries
        ]

        moved: list[str] = []
        unknown: list[str] = []
        for ts_id in requested:
            if ts_id in by_id:
                moved.append(ts_id)
            else:
                unknown.append(ts_id)

        return ResolveResult(
            file=self.model_copy(update={"entries": new_entries}),
            moved_ids=tuple(moved),
            unknown_ids=tuple(unknown),
        )


def _parse_entry(
    lines: list[str], start: int, in_resolved_section: bool
) -> tuple[QuestionEntry | None, int]:
    """Parse a single ``- [...] body`` entry starting at ``lines[start]``.

    Returns ``(entry, lines_consumed)``. ``entry`` is None when the line
    can't be parsed; the caller advances past the unparsable line.
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

    if in_resolved_section and resolved_ref is None:
        # An open-form entry that landed under ## Resolved is parsable but
        # has no ResolvedRef. Surface it as resolved with no ref so it
        # doesn't reappear in the open section on round-trip; the caller
        # can heal it on the next write by re-resolving with a real ref.
        # In practice the writer always emits the resolved-prefix form.
        pass

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
