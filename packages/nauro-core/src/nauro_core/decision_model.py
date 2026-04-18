"""Pydantic v2 Decision model + canonical YAML-frontmatter round-trip.

The authoritative shape for a parsed decision. `parse_decision_v2` reads a
markdown file with YAML frontmatter and returns a validated `Decision`.
`format_decision_v2` goes the other way.

Strict by design — per the migration plan §9, unknown enum values, missing
required fields, malformed YAML, non-ISO dates, reasonless rejected
alternatives on active decisions, and superseded decisions without a
`superseded_by` ref all raise. Legacy tolerance lives in the Phase 3 migration
script, not here.

Field model (see plan §2):
    Required frontmatter: date, confidence.
    Defaulted frontmatter: version (1), status (active).
    Optional frontmatter: decision_type, reversibility, source, files_affected,
                          supersedes, superseded_by.
    Body-rendered: rejected (rendered as `## Rejected Alternatives` + `### name`
                   subsections, not in frontmatter).
    Derived (set by parse_decision_v2): num, title, rationale, body, content.
"""

from __future__ import annotations

import re
from datetime import date as _date
from enum import StrEnum

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nauro_core.parsing import extract_decision_number

# ── Enums (values match on-disk lowercase tokens verbatim) ──


class DecisionStatus(StrEnum):
    active = "active"
    superseded = "superseded"


class DecisionConfidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class DecisionType(StrEnum):
    architecture = "architecture"
    api_design = "api_design"
    infrastructure = "infrastructure"
    pattern = "pattern"
    refactor = "refactor"
    data_model = "data_model"


class Reversibility(StrEnum):
    easy = "easy"
    moderate = "moderate"
    hard = "hard"


class DecisionSource(StrEnum):
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

    # ── Derived (set by parse_decision_v2; excluded from frontmatter dump) ──
    num: int = Field(default=0, ge=0)
    title: str = ""
    rationale: str = ""
    body: str = ""
    content: str = ""

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
# format_decision_v2 applies it; if this ends up being applied in a second
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
_SECTION_START = re.compile(r"^##\s+", re.MULTILINE)
_SUBSECTION_SPLIT = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def parse_decision_v2(text: str, filename: str) -> Decision:
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

    rationale = _extract_section_body(body, "Decision")
    if rationale is None:
        raise ValueError(
            f"{filename}: missing `## Decision` section "
            "(v2 does not accept `## Rationale` — Phase 3 migration renames "
            "legacy files; the parser itself stays strict)"
        )

    rejected_body = _extract_section_body(body, "Rejected Alternatives")
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


def _extract_section_body(body: str, heading: str) -> str | None:
    """Return the stripped body of a ``## {heading}`` section, or None.

    Scans for `## heading` followed by newline; returns everything up to the
    next `##` heading (or end of body).
    """
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    m = pattern.search(body)
    if not m:
        return None
    start = m.end()
    next_section = _SECTION_START.search(body[start:])
    section = body[start : start + next_section.start()] if next_section else body[start:]
    return section.strip()


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


def format_decision_v2(decision: Decision) -> str:
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

    Idempotent with ``parse_decision_v2``: format → parse → format is byte-identical.
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
    "Decision",
    "DecisionConfidence",
    "DecisionSource",
    "DecisionStatus",
    "DecisionType",
    "RejectedAlternative",
    "Reversibility",
    "format_decision_v2",
    "parse_decision_v2",
]
