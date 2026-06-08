"""Pydantic Decision model + canonical YAML-frontmatter round-trip.

The authoritative shape for a parsed decision. `parse_decision` reads a
markdown file with YAML frontmatter and returns a validated `Decision`.
`format_decision` goes the other way.

Strict by design — per the migration plan §9, unknown enum values, missing
required fields, malformed YAML, non-ISO dates, reasonless rejected
alternatives on active decisions, and superseded decisions without a
`superseded_by` ref all raise.

Supersession refs (`supersedes`, `superseded_by`) are validated as plain
integer strings: "70", not "070" or "070-some-slug" or "D70".

Field model (see plan §2):
    Required frontmatter: date, confidence.
    Defaulted frontmatter: version (1), status (active).
    Optional frontmatter: decision_type, reversibility, source, files_affected,
                          supersedes, superseded_by.
    Body-rendered: rejected (rendered as `## Rejected Alternatives` + `### name`
                   subsections, not in frontmatter).
    Derived (set by parse_decision): num, title, rationale, body, content.
"""

from __future__ import annotations

import re
from datetime import date as _date
from enum import Enum

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nauro_core.parsing import extract_decision_number

# ── Enums (values match on-disk lowercase tokens verbatim) ──


class DecisionStatus(str, Enum):
    active = "active"
    superseded = "superseded"


class DecisionConfidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class DecisionType(str, Enum):
    architecture = "architecture"
    api_design = "api_design"
    infrastructure = "infrastructure"
    pattern = "pattern"
    refactor = "refactor"
    data_model = "data_model"


# Canonical list of advertised decision-type tokens, derived from the
# DecisionType enum so the schema copies that surface these values (the MCP
# input schema, the public constants re-export) cannot drift from the
# validator that actually accepts them. decision_model is the lowest module in
# the import chain (decision_model -> parsing -> constants -> protocol), so this
# value is imported outward; constants cannot import it without a cycle.
DECISION_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in DecisionType)


class Reversibility(str, Enum):
    easy = "easy"
    moderate = "moderate"
    hard = "hard"


class DecisionSource(str, Enum):
    mcp = "mcp"
    commit = "commit"
    compaction = "compaction"
    manual = "manual"
    # `import` is a Python keyword, so the member is named `import_` but its
    # value serializes as the string "import".
    import_ = "import"


# ── Models ──


class RejectedAlternative(BaseModel):
    """A rejected alternative considered alongside the chosen decision.

    `reason` is optional on the model but required-in-practice for active
    decisions: Decision.require_reasons_on_active enforces it at the
    composite level.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    reason: str | None = None


class Decision(BaseModel):
    """Parsed, validated representation of a decision markdown file.

    Frontmatter fields round-trip through YAML. Derived fields (``num``,
    ``title``, ``rationale``, ``body``, ``content``) are populated by the
    parser from the markdown body and the filename.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ── Frontmatter: required ──
    date: _date
    confidence: DecisionConfidence

    # ── Frontmatter: defaulted ──
    version: int = Field(default=1, ge=1)
    status: DecisionStatus = DecisionStatus.active

    # ── Frontmatter: semantically optional ──
    decision_type: DecisionType | None = None
    reversibility: Reversibility | None = None
    source: DecisionSource | None = None
    files_affected: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None

    # ── Body-rendered ──
    rejected: list[RejectedAlternative] = Field(default_factory=list)

    # ── Derived (set by parse_decision; excluded from frontmatter dump) ──
    num: int = Field(default=0, ge=0)
    title: str = ""
    rationale: str = ""
    body: str = ""
    content: str = ""

    @field_validator("supersedes", "superseded_by")
    @classmethod
    def _validate_supersession_ref(cls, v: str | None) -> str | None:
        """Enforce plain integer string: "70", not "070" or "070-slug" or "D70".

        Canonicalizes the format that ``writer.supersede_decision`` writes.
        """
        if v is None:
            return v
        if not v.isdigit():
            raise ValueError(
                f"supersession ref must be a plain integer string (e.g. '70'), got {v!r}"
            )
        canonical = str(int(v))
        if v != canonical:
            raise ValueError(
                f"supersession ref must not have leading zeros, got {v!r}; expected {canonical!r}"
            )
        return v

    @model_validator(mode="after")
    def require_reasons_on_active(self) -> Decision:
        if self.status is DecisionStatus.active:
            reasonless = [r.name for r in self.rejected if r.reason is None]
            if reasonless:
                raise ValueError(
                    "Active decision has rejected alternatives without reasons: "
                    f"{reasonless}. Reasonless rejections are decision-hygiene "
                    "failures."
                )
        return self

    @model_validator(mode="after")
    def superseded_requires_ref(self) -> Decision:
        if self.status is DecisionStatus.superseded and not self.superseded_by:
            raise ValueError(
                "status=superseded requires superseded_by to point at the replacing decision."
            )
        return self


# ── Frontmatter exclusion set (plan §12.2) ──
#
# Fields derived from the filename or body that must NOT appear in the
# frontmatter dump. Kept as a module-level constant because
# format_decision applies it; if this ends up being applied in a second
# place (snapshot v2, a remote payload serializer), split the model into
# DecisionMetadata + Decision per plan §12.2.
_DERIVED_FIELDS: frozenset[str] = frozenset({"num", "title", "rationale", "body", "content"})

# Canonical key order for the frontmatter block. `rejected` is not in this
# list — it is popped from the dump dict before yaml serialization and
# rendered as markdown body subsections.
_FRONTMATTER_ORDER: tuple[str, ...] = (
    "date",
    "version",
    "status",
    "confidence",
    "decision_type",
    "reversibility",
    "source",
    "files_affected",
    "supersedes",
    "superseded_by",
)


# ── Parser ──


_H1_PATTERN = re.compile(r"^#\s+(\d+)\s+\u2014\s+(.+)$", re.MULTILINE)
_SUBSECTION_SPLIT = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)

_DECISION_ANCHOR = "## Decision"
_REJECTED_ANCHOR = "## Rejected Alternatives"


def parse_decision(text: str, filename: str) -> Decision:
    """Parse a decision markdown file into a validated ``Decision``.

    Strict by design: raises ``ValueError`` (and propagates
    ``pydantic.ValidationError``) on any deviation from the canonical
    v2 format. Migration-time tolerance lives in the Phase 3 script.

    Raises:
        ValueError: missing/unterminated frontmatter, missing H1, missing
            `## Decision` section, malformed YAML, frontmatter not a mapping.
        pydantic.ValidationError: any field-level validation failure.
    """
    if not text.startswith("---\n"):
        raise ValueError(
            f"{filename}: missing YAML frontmatter (decisions v2 requires a leading `---` fence)"
        )
    fm_end = text.find("\n---\n", 4)
    if fm_end == -1:
        raise ValueError(f"{filename}: unterminated YAML frontmatter")

    frontmatter_block = text[4:fm_end]
    body = text[fm_end + 5 :]

    try:
        metadata = yaml.safe_load(frontmatter_block)
    except yaml.YAMLError as e:
        raise ValueError(f"{filename}: invalid YAML frontmatter: {e}") from e

    if metadata is None:
        metadata = {}
    elif not isinstance(metadata, dict):
        raise ValueError(
            f"{filename}: frontmatter must be a mapping, got {type(metadata).__name__}"
        )

    h1 = _H1_PATTERN.search(body)
    if not h1:
        raise ValueError(
            f"{filename}: missing or malformed H1 "
            "(expected `# NNN \u2014 Title` with em-dash separator)"
        )
    title = h1.group(2).strip()
    file_num = extract_decision_number(filename) or 0

    rationale, rejected_body = _split_decision_body(body)
    if rationale is None:
        raise ValueError(
            f"{filename}: missing `## Decision` section "
            "(v2 does not accept `## Rationale` — Phase 3 migration renames "
            "legacy files; the parser itself stays strict)"
        )

    rejected_list = _parse_rejected_subsections(rejected_body or "")

    # Body-rendered field goes in via the normal constructor path so the
    # validators run. Derived fields are passed positionally for clarity.
    return Decision(
        **metadata,
        rejected=rejected_list,
        num=file_num,
        title=title,
        rationale=rationale,
        body=body,
        content=text,
    )


def _split_decision_body(body: str) -> tuple[str | None, str | None]:
    """Split a decision body into ``(rationale, rejected_body)``.

    The rationale may contain arbitrary ``##``/``###`` subsections, ``---``
    rules, and fenced code blocks. Only two top-level headings are treated as
    section boundaries, and only when they appear as non-fenced whole lines:

    - The Decision anchor is the FIRST non-fenced line whose stripped form is
      exactly ``## Decision``. If none exists, the rationale is ``None`` and the
      caller raises the missing-section error (contract unchanged).
    - The Rejected anchor is the LAST non-fenced whole-line ``## Rejected
      Alternatives`` occurring after the Decision anchor. A literal heading line
      inside the rationale (e.g. an example, or one preceding the real block) is
      therefore kept in the rationale, and only the genuine trailing block is
      parsed as rejected alternatives.

    Lines inside fenced code blocks (toggled by ``` ``` ``` or ``~~~``) never
    anchor, and the fence-marker lines themselves are never anchors.

    Returns:
        ``(rationale, rejected_body)`` where ``rationale`` is the stripped text
        between the Decision anchor and the Rejected anchor (or end of body),
        and ``rejected_body`` is the stripped text after the Rejected anchor, or
        ``None`` when there is no Rejected anchor.
    """
    lines = body.split("\n")

    in_fence = False
    decision_idx: int | None = None
    rejected_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        trimmed = line.rstrip()
        if decision_idx is None:
            if trimmed == _DECISION_ANCHOR:
                decision_idx = i
            continue
        if trimmed == _REJECTED_ANCHOR:
            rejected_idx = i

    if decision_idx is None:
        return None, None

    if rejected_idx is None:
        rationale = "\n".join(lines[decision_idx + 1 :]).strip()
        return rationale, None

    rationale = "\n".join(lines[decision_idx + 1 : rejected_idx]).strip()
    rejected_body = "\n".join(lines[rejected_idx + 1 :]).strip()
    return rationale, rejected_body


def _parse_rejected_subsections(section_text: str) -> list[RejectedAlternative]:
    """Parse `### name` subsections into RejectedAlternative objects.

    `reason` is the stripped body below each `### name` heading; None if empty.
    """
    if not section_text.strip():
        return []

    chunks = _SUBSECTION_SPLIT.split(section_text)
    # chunks[0] is preamble before the first `###`; chunks alternate
    # [preamble, name1, body1, name2, body2, ...]
    alternatives: list[RejectedAlternative] = []
    for i in range(1, len(chunks), 2):
        name = chunks[i].strip()
        reason_raw = chunks[i + 1].strip() if i + 1 < len(chunks) else ""
        alternatives.append(RejectedAlternative(name=name, reason=reason_raw or None))
    return alternatives


# ── Formatter ──


def format_decision(decision: Decision) -> str:
    """Serialize a ``Decision`` to canonical v2 markdown.

    Output shape:
        ---
        <ordered YAML frontmatter>
        ---

        # NNN — Title

        ## Decision

        {rationale}

        ## Rejected Alternatives   (only if any)

        ### name

        {reason}

    Idempotent with ``parse_decision``: format → parse → format is byte-identical.
    """
    dumped = decision.model_dump(mode="json", exclude=_DERIVED_FIELDS)

    # Rejected is body-rendered, not frontmatter.
    dumped.pop("rejected", None)

    # pydantic's mode="json" converts date → ISO string. yaml.safe_dump
    # would quote that; we prefer unquoted `date: 2026-04-16`, so convert back.
    if dumped.get("date") is not None:
        dumped["date"] = _date.fromisoformat(dumped["date"])

    ordered = {k: dumped[k] for k in _FRONTMATTER_ORDER if k in dumped}

    yaml_block = yaml.safe_dump(
        ordered,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    sections: list[str] = [
        f"---\n{yaml_block}---",
        f"# {decision.num:03d} \u2014 {decision.title}",
        f"## Decision\n\n{decision.rationale.strip()}",
    ]

    if decision.rejected:
        rejected_parts = ["## Rejected Alternatives"]
        for r in decision.rejected:
            rejected_parts.append(f"### {r.name}")
            if r.reason:
                rejected_parts.append(r.reason.strip())
        sections.append("\n\n".join(rejected_parts))

    return "\n\n".join(sections) + "\n"


__all__ = [
    "DECISION_TYPE_VALUES",
    "Decision",
    "DecisionConfidence",
    "DecisionSource",
    "DecisionStatus",
    "DecisionType",
    "RejectedAlternative",
    "Reversibility",
    "format_decision",
    "parse_decision",
]
