"""Tests for nauro_core.decision_model — the v2 pydantic Decision model.

Coverage:
    - Round-trip: parse → format → parse round-trips a representative set of
      real-store shapes (empty rejected list, populated rejected with special
      chars, populated files_affected list).
    - Idempotence: format → parse → format is byte-identical on all fixtures.
    - Negative: every validation rule raises.
    - Positive: optional fields accept None / empty cleanly.

Fixtures are synthesized inline because no file in the current store is in v2
format yet — Phase 3 migration will produce v2 output. Each fixture is
documented with a reference to the real decision it is modeled on.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
    parse_decision,
)

# ── Fixtures: three representative v2 shapes ──

# Fixture 1 — pure choice, no rejected alternatives.
# Modeled on a simple active-decision shape (equivalent to an early store
# decision that chose between options but documented only the chosen path).
MINIMAL_V2 = """\
---
date: 2026-04-01
version: 1
status: active
confidence: medium
decision_type: pattern
reversibility: easy
source: mcp
files_affected: []
supersedes: null
superseded_by: null
---

# 042 \u2014 Use a shared helper for slug generation

## Decision

Extract the slug-truncation logic into nauro_core so both the CLI writer and
the remote MCP server emit identical filenames. Avoids drift across the two
producers.
"""
MINIMAL_V2_FILENAME = "042-use-a-shared-helper-for-slug-generation.md"


# Fixture 2 — multiple rejected alternatives, names include an em-dash and a
# colon (the kind of content D98 carries in the real store).
RICH_V2 = """\
---
date: 2026-04-16
version: 1
status: active
confidence: high
decision_type: infrastructure
reversibility: moderate
source: mcp
files_affected:
- mcp-server/pyproject.toml
- mcp-server/uv.lock
supersedes: null
superseded_by: null
---

# 098 \u2014 Private-forever mcp-server uses git-ref dep on nauro-core

## Decision

Private repos can depend on public-repo source via git refs without round-tripping
through PyPI for every change. The Lambda build still pulls the published wheel
for manylinux determinism; dev and CI read from a pinned commit.

## Rejected Alternatives

### Monorepo \u2014 fold mcp-server into the nauro repo

Loses the private/public boundary. mcp-server contains auth logic and
operational secrets that cannot ship to a public tree.

### PyPI round-trip on every change

Forces a publish + wait loop on every shared-code edit. Acceptable for external
consumers, not for a private repo the same author owns.
"""
RICH_V2_FILENAME = "098-private-forever-mcp-server-uses-git-ref-dep.md"


# Fixture 3 — populated files_affected with multiple paths.
FILES_AFFECTED_V2 = """\
---
date: 2026-03-30
version: 1
status: active
confidence: high
decision_type: architecture
reversibility: easy
source: commit
files_affected:
- src/nauro/sync/daemon.py
- src/nauro/sync/remote.py
- src/nauro/sync/state.py
supersedes: null
superseded_by: null
---

# 042 \u2014 Replace sync daemon with explicit git-style sync

## Decision

Explicit push and pull triggered by meaningful moments (session start,
post-commit) replaces the 30s polling daemon. Lower complexity, fewer moving
parts, same user-visible freshness guarantees.

## Rejected Alternatives

### Keep the 30-second polling daemon

Solves a low-frequency problem with a high-frequency mechanism.

### Real-time sync via S3 event notifications

Engineering overkill for a solo-developer usage pattern.
"""
FILES_AFFECTED_V2_FILENAME = "042-replace-sync-daemon-with-explicit-git-style-sync.md"


ALL_FIXTURES: list[tuple[str, str]] = [
    (MINIMAL_V2, MINIMAL_V2_FILENAME),
    (RICH_V2, RICH_V2_FILENAME),
    (FILES_AFFECTED_V2, FILES_AFFECTED_V2_FILENAME),
]


# ── Round-trip tests ──


class TestRoundTrip:
    @pytest.mark.parametrize("text,filename", ALL_FIXTURES)
    def test_parse_format_parse(self, text: str, filename: str) -> None:
        """parse → format → parse returns an equivalent Decision."""
        first = parse_decision(text, filename)
        formatted = format_decision(first)
        second = parse_decision(formatted, filename)

        # Equality of metadata, not of derived `content` (which includes the
        # original verbatim text and may differ from the formatted output
        # whitespace-wise).
        assert first.date == second.date
        assert first.version == second.version
        assert first.status == second.status
        assert first.confidence == second.confidence
        assert first.decision_type == second.decision_type
        assert first.reversibility == second.reversibility
        assert first.source == second.source
        assert first.files_affected == second.files_affected
        assert first.supersedes == second.supersedes
        assert first.superseded_by == second.superseded_by
        assert first.rejected == second.rejected
        assert first.num == second.num
        assert first.title == second.title
        assert first.rationale == second.rationale

    @pytest.mark.parametrize("text,filename", ALL_FIXTURES)
    def test_format_parse_format_is_byte_identical(self, text: str, filename: str) -> None:
        """Idempotence: once reformatted, subsequent rounds produce identical bytes."""
        first_decision = parse_decision(text, filename)
        once = format_decision(first_decision)
        twice = format_decision(parse_decision(once, filename))
        assert once == twice, (
            f"format_decision is not idempotent:\n--- once ---\n{once}\n--- twice ---\n{twice}\n"
        )


class TestRejectedAlternativeRoundTrip:
    def test_special_chars_in_name(self) -> None:
        """Em-dashes and colons in rejected names survive round-trip."""
        decision = parse_decision(RICH_V2, RICH_V2_FILENAME)
        names = [r.name for r in decision.rejected]
        assert "Monorepo \u2014 fold mcp-server into the nauro repo" in names
        assert "PyPI round-trip on every change" in names

        formatted = format_decision(decision)
        reparsed = parse_decision(formatted, RICH_V2_FILENAME)
        assert [r.name for r in reparsed.rejected] == names
        assert [r.reason for r in reparsed.rejected] == [r.reason for r in decision.rejected]


# ── Positive: optional fields accept None / empty ──


class TestOptionalFields:
    def test_empty_files_affected(self) -> None:
        d = parse_decision(MINIMAL_V2, MINIMAL_V2_FILENAME)
        assert d.files_affected == []

    def test_null_supersedes(self) -> None:
        d = parse_decision(MINIMAL_V2, MINIMAL_V2_FILENAME)
        assert d.supersedes is None
        assert d.superseded_by is None

    def test_no_rejected_alternatives(self) -> None:
        d = parse_decision(MINIMAL_V2, MINIMAL_V2_FILENAME)
        assert d.rejected == []

    def test_defaults_when_fields_omitted(self) -> None:
        """version and status have defaults; parsing a minimal frontmatter works."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 001 \u2014 Minimal decision\n\n"
            "## Decision\n\nChose option A.\n"
        )
        d = parse_decision(text, "001-minimal.md")
        assert d.version == 1
        assert d.status is DecisionStatus.active
        assert d.decision_type is None
        assert d.source is None


# ── Negative: strict validation ──


class TestNegativeValidation:
    def _build(self, **overrides: object) -> str:
        """Build a frontmatter+body text with overrideable metadata."""
        defaults: dict[str, object] = {
            "date": "2026-04-01",
            "version": 1,
            "status": "active",
            "confidence": "high",
        }
        defaults.update(overrides)
        fm_lines = [f"{k}: {v}" for k, v in defaults.items() if v is not None]
        fm = "\n".join(fm_lines)
        return (
            f"---\n{fm}\n---\n\n"
            "# 001 \u2014 Test decision\n\n"
            "## Decision\n\nSomething was chosen.\n"
        )

    def test_unknown_status_raises(self) -> None:
        text = self._build(status="experimental")
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_unknown_confidence_raises(self) -> None:
        text = self._build(confidence="super_high")
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_unknown_decision_type_raises(self) -> None:
        text = self._build(decision_type="library_choice")  # removed from enum
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_unknown_reversibility_raises(self) -> None:
        text = self._build(reversibility="impossible")
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_unknown_source_raises(self) -> None:
        text = self._build(source="oracle")
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_missing_required_confidence_raises(self) -> None:
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "---\n\n"
            "# 001 \u2014 No confidence\n\n"
            "## Decision\n\nSomething.\n"
        )
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_missing_required_date_raises(self) -> None:
        text = "---\nconfidence: high\n---\n\n# 001 \u2014 No date\n\n## Decision\n\nSomething.\n"
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_non_iso_date_raises(self) -> None:
        text = (
            "---\n"
            "date: '04/01/2026'\n"
            "confidence: high\n"
            "---\n\n"
            "# 001 \u2014 Bad date\n\n"
            "## Decision\n\nSomething.\n"
        )
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")

    def test_malformed_yaml_raises(self) -> None:
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "files_affected: [unterminated\n"  # missing closing bracket
            "---\n\n"
            "# 001 \u2014 Bad yaml\n\n"
            "## Decision\n\nSomething.\n"
        )
        with pytest.raises(ValueError, match="invalid YAML"):
            parse_decision(text, "001-test.md")

    def test_missing_frontmatter_raises(self) -> None:
        text = "# 001 \u2014 No frontmatter\n\n## Decision\n\nSomething.\n"
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            parse_decision(text, "001-test.md")

    def test_unterminated_frontmatter_raises(self) -> None:
        text = "---\ndate: 2026-04-01\nconfidence: high\n\n# 001 \u2014 Title\n"
        with pytest.raises(ValueError, match="unterminated"):
            parse_decision(text, "001-test.md")

    def test_missing_h1_raises(self) -> None:
        text = "---\ndate: 2026-04-01\nconfidence: high\n---\n\n## Decision\n\nNo H1 above.\n"
        with pytest.raises(ValueError, match="missing or malformed H1"):
            parse_decision(text, "001-test.md")

    def test_h1_with_colon_separator_raises(self) -> None:
        """Parser is strict: colon-H1 is legacy, must be migrated to em-dash first."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 001: Old-style colon heading\n\n"
            "## Decision\n\nSomething.\n"
        )
        with pytest.raises(ValueError, match="missing or malformed H1"):
            parse_decision(text, "001-test.md")

    def test_missing_decision_section_raises(self) -> None:
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 001 \u2014 No decision section\n\n"
            "Some prose but no `## Decision` heading.\n"
        )
        with pytest.raises(ValueError, match="missing `## Decision`"):
            parse_decision(text, "001-test.md")

    def test_rationale_heading_alone_raises(self) -> None:
        """`## Rationale` is legacy — parser does not accept it."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 001 \u2014 Legacy rationale\n\n"
            "## Rationale\n\nLegacy prose.\n"
        )
        with pytest.raises(ValueError, match="missing `## Decision`"):
            parse_decision(text, "001-test.md")

    def test_reasonless_rejected_on_active_raises(self) -> None:
        """An active decision cannot have reasonless rejected alternatives."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "status: active\n"
            "---\n\n"
            "# 001 \u2014 Active with naked rejection\n\n"
            "## Decision\n\nPicked A.\n\n"
            "## Rejected Alternatives\n\n"
            "### B\n"  # no reason below
        )
        with pytest.raises(ValidationError, match="without reasons"):
            parse_decision(text, "001-test.md")

    def test_superseded_without_superseded_by_raises(self) -> None:
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "status: superseded\n"
            "---\n\n"
            "# 001 \u2014 Orphaned supersession\n\n"
            "## Decision\n\nOld decision.\n"
        )
        with pytest.raises(ValidationError, match="superseded_by"):
            parse_decision(text, "001-test.md")

    def test_extra_frontmatter_key_raises(self) -> None:
        """extra='forbid' on Decision means typos fail loudly."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "confidance: high\n"  # typo
            "---\n\n"
            "# 001 \u2014 Typo in frontmatter\n\n"
            "## Decision\n\nSomething.\n"
        )
        with pytest.raises(ValidationError):
            parse_decision(text, "001-test.md")


# ── Model-level construction tests (no parsing) ──


def _minimal_decision(**overrides: object) -> Decision:
    """Build a Decision with the minimum required fields, overrides applied."""
    kwargs: dict[str, object] = {
        "date": date(2026, 4, 1),
        "confidence": DecisionConfidence.high,
        "num": 1,
        "title": "Test",
        "rationale": "Chose A.",
    }
    kwargs.update(overrides)
    return Decision(**kwargs)  # type: ignore[arg-type]


class TestDecisionConstruction:
    def test_superseded_with_ref_ok(self) -> None:
        d = _minimal_decision(
            status=DecisionStatus.superseded,
            superseded_by="42",
        )
        assert d.status is DecisionStatus.superseded
        assert d.superseded_by == "42"

    def test_source_import_serializes_correctly(self) -> None:
        d = _minimal_decision(source=DecisionSource.import_)
        dumped = d.model_dump(mode="json")
        assert dumped["source"] == "import"

    def test_all_decision_types_accepted(self) -> None:
        for dt in DecisionType:
            d = _minimal_decision(decision_type=dt)
            assert d.decision_type is dt

    def test_all_reversibilities_accepted(self) -> None:
        for r in Reversibility:
            d = _minimal_decision(reversibility=r)
            assert d.reversibility is r

    def test_all_sources_accepted(self) -> None:
        for s in DecisionSource:
            d = _minimal_decision(source=s)
            assert d.source is s

    def test_rejected_alternative_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            RejectedAlternative(name="")  # min_length=1

    def test_rejected_alternative_reason_optional(self) -> None:
        r = RejectedAlternative(name="Thing")
        assert r.reason is None


class TestSupersessionRefValidator:
    """The supersedes / superseded_by validator: plain integer string only.

    Convention is "70", not "070" or "070-some-slug" or "D70". The prior
    session's D69/D70/D105 backfill standardized on this form, and
    writer.supersede_decision now canonicalizes filename stems before
    writing. The model-level validator is the backstop.
    """

    def test_plain_integer_accepted(self) -> None:
        d = _minimal_decision(supersedes="70")
        assert d.supersedes == "70"

    def test_none_accepted(self) -> None:
        d = _minimal_decision(supersedes=None, superseded_by=None)
        assert d.supersedes is None
        assert d.superseded_by is None

    def test_leading_zeros_rejected(self) -> None:
        with pytest.raises(ValidationError, match="leading zeros"):
            _minimal_decision(supersedes="070")

    def test_filename_stem_rejected(self) -> None:
        with pytest.raises(ValidationError, match="plain integer string"):
            _minimal_decision(supersedes="42-some-slug")

    def test_d_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError, match="plain integer string"):
            _minimal_decision(supersedes="D70")

    def test_validator_applies_to_superseded_by_too(self) -> None:
        with pytest.raises(ValidationError, match="leading zeros"):
            _minimal_decision(
                status=DecisionStatus.superseded,
                superseded_by="070",
            )
