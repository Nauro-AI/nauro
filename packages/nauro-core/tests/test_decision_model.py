"""Tests for nauro_core.decision_model — the v2 pydantic Decision model.

Coverage:
    - Round-trip: parse → format → parse round-trips a representative set of
      real-store shapes (empty rejected list, populated rejected with special
      chars, populated files_affected list).
    - Idempotence: format → parse → format is byte-identical on all fixtures.
    - Negative: every validation rule raises.
    - Positive: optional fields accept None / empty cleanly.

Fixtures are synthesized inline rather than read from the store, so the suite
does not depend on store contents. Each fixture is documented with a reference
to the real decision it is modeled on.
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
    _split_frontmatter,
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
# colon.
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


# ── Rationale boundary: subsections, fences, whole-line anchoring ──


class TestRationaleBoundary:
    """The rationale may contain arbitrary `##`/`###`/`---` and fenced code.

    The parser anchors only on non-fenced whole-line `## Decision` and the
    last non-fenced whole-line `## Rejected Alternatives`.
    """

    def test_rationale_with_subsections_preserved(self) -> None:
        """A rationale with `## Tradeoffs`, `### Detail`, `---`, and trailing
        prose is preserved in full, and reformat is byte-identical idempotent."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 050 — Decision with rich rationale\n\n"
            "## Decision\n\n"
            "Lead paragraph describing the chosen path.\n\n"
            "## Tradeoffs\n\n"
            "We accept slower cold starts for simpler code.\n\n"
            "### Detail\n\n"
            "The fan-out adds bounded latency.\n\n"
            "---\n\n"
            "Trailing prose after a horizontal rule.\n"
        )
        d = parse_decision(text, "050-rich-rationale.md")
        assert "## Tradeoffs" in d.rationale
        assert "### Detail" in d.rationale
        assert "---" in d.rationale
        assert "Trailing prose after a horizontal rule." in d.rationale
        # Not truncated to the lead paragraph.
        assert d.rationale != "Lead paragraph describing the chosen path."
        assert d.rejected == []

        once = format_decision(d)
        twice = format_decision(parse_decision(once, "050-rich-rationale.md"))
        assert once == twice

    def test_literal_rejected_heading_in_rationale_not_fabricated(self) -> None:
        """A literal `## Rejected Alternatives` + `### Fake` mid-rationale must
        stay in the rationale; only the REAL trailing section is parsed."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 051 — Worst case literal heading\n\n"
            "## Decision\n\n"
            "We discuss a hypothetical below.\n\n"
            "## Rejected Alternatives\n\n"
            "### Fake\n\n"
            "This is prose inside the rationale, not a real rejection.\n\n"
            "## Rejected Alternatives\n\n"
            "### Real Option\n\n"
            "The genuinely-rejected alternative, with its reason.\n"
        )
        d = parse_decision(text, "051-worst-case.md")
        # The literal earlier heading and its fake subsection stay in rationale.
        assert "## Rejected Alternatives" in d.rationale
        assert "### Fake" in d.rationale
        assert "This is prose inside the rationale, not a real rejection." in d.rationale
        # Only the real option is parsed; no fabricated "Fake" record.
        assert [r.name for r in d.rejected] == ["Real Option"]

    def test_midline_heading_mention_does_not_truncate(self) -> None:
        """A mid-line mention of the heading words must not anchor."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 052 — Mid-line mention\n\n"
            "## Decision\n\n"
            "We reference the ## Decision marker inline and also a phrase like "
            "the ## Rejected Alternatives heading mid-sentence.\n\n"
            "More rationale follows the inline mentions.\n"
        )
        d = parse_decision(text, "052-midline.md")
        assert "More rationale follows the inline mentions." in d.rationale
        assert "## Rejected Alternatives heading mid-sentence" in d.rationale
        assert d.rejected == []

    def test_fenced_rejected_heading_not_anchor(self) -> None:
        """A fenced code block containing a `## Rejected Alternatives` line with
        NO real rejected section: the fenced heading is not an anchor, the full
        rationale (including the fence) is preserved, rejected == []."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 053 — Fenced heading only\n\n"
            "## Decision\n\n"
            "We show a sample document below.\n\n"
            "```markdown\n"
            "## Rejected Alternatives\n\n"
            "### Not Real\n"
            "```\n\n"
            "Closing rationale after the fence.\n"
        )
        d = parse_decision(text, "053-fenced-only.md")
        assert "```markdown" in d.rationale
        assert "## Rejected Alternatives" in d.rationale
        assert "### Not Real" in d.rationale
        assert "Closing rationale after the fence." in d.rationale
        assert d.rejected == []

    def test_fenced_heading_plus_real_section(self) -> None:
        """A fenced `## Rejected Alternatives` inside the rationale AND a real
        rejected section after the fence: the real section is the anchor."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 054 — Fence plus real section\n\n"
            "## Decision\n\n"
            "Here is an illustrative document:\n\n"
            "```\n"
            "## Rejected Alternatives\n"
            "### Illustrative Only\n"
            "```\n\n"
            "And here is the actual reasoning.\n\n"
            "## Rejected Alternatives\n\n"
            "### Genuine Option\n\n"
            "The real reason this was rejected.\n"
        )
        d = parse_decision(text, "054-fence-plus-real.md")
        # Fenced heading stays in the rationale.
        assert "### Illustrative Only" in d.rationale
        assert "And here is the actual reasoning." in d.rationale
        # Only the real, post-fence section is parsed.
        assert [r.name for r in d.rejected] == ["Genuine Option"]
        assert d.rejected[0].reason == "The real reason this was rejected."

    def test_mixed_marker_fence_does_not_desync_rejected_split(self) -> None:
        """A ```text fence whose body contains a single bare ~~~~~ line must not
        desync the fence tracker. With a single in_fence boolean the tilde run
        toggled the tracker a second time, so the parser believed it was still
        inside a fence when the real `## Rejected Alternatives` heading arrived
        and silently dropped the whole section. Closing only on the matching
        marker keeps the trailing rejected block parseable."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 056 — Mixed fence markers\n\n"
            "## Decision\n\n"
            "We document the separator convention below.\n\n"
            "```text\n"
            "~~~~~\n"
            "```\n\n"
            "And then the actual reasoning.\n\n"
            "## Rejected Alternatives\n\n"
            "### Genuine Option\n\n"
            "The real reason this was rejected.\n"
        )
        d = parse_decision(text, "056-mixed-fence.md")
        # The tilde run is fence content, so it stays in the rationale.
        assert "~~~~~" in d.rationale
        assert "And then the actual reasoning." in d.rationale
        # The trailing rejected section survives rather than being swallowed.
        assert len(d.rejected) == 1
        assert d.rejected[0].name == "Genuine Option"
        assert d.rejected[0].reason == "The real reason this was rejected."

    def test_no_rejected_section_full_rationale(self) -> None:
        """No rejected section: rejected == [] and the full rationale is kept."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 055 — No rejected\n\n"
            "## Decision\n\n"
            "First paragraph.\n\n"
            "## Context\n\n"
            "Second paragraph with a subsection heading.\n"
        )
        d = parse_decision(text, "055-no-rejected.md")
        assert "## Context" in d.rationale
        assert "Second paragraph with a subsection heading." in d.rationale
        assert d.rejected == []

    def test_fenced_decision_heading_alone_raises(self) -> None:
        """A `## Decision` that appears only inside a fenced code block must not
        anchor. With no real section, the missing-section error is raised."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 057 — Fenced decision only\n\n"
            "Here is a sample document, not the real section:\n\n"
            "```markdown\n"
            "## Decision\n\n"
            "Sample body inside the fence.\n"
            "```\n\n"
            "Prose but no real Decision heading.\n"
        )
        with pytest.raises(ValueError, match="missing `## Decision`"):
            parse_decision(text, "057-fenced-decision-only.md")

    def test_fenced_fake_decision_before_real_anchors_on_real(self) -> None:
        """A fenced fake `## Decision` before the real one must not anchor; the
        real, non-fenced heading is the anchor and the body parses cleanly."""
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 058 — Fenced fake before real\n\n"
            "```markdown\n"
            "## Decision\n\n"
            "This is a documentation sample, not the real section.\n"
            "```\n\n"
            "## Decision\n\n"
            "The real chosen path lives here.\n"
        )
        d = parse_decision(text, "058-fenced-fake-before-real.md")
        # The fenced sample precedes the anchor, so the rationale is exactly the
        # real body below the genuine heading.
        assert d.rationale == "The real chosen path lives here."
        assert d.rejected == []


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
        # "library_choice" is not a DecisionType member, so the validator must
        # reject it. The advertised schema copies no longer offer it either.
        text = self._build(decision_type="library_choice")
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

    Convention is "70", not "070" or "070-some-slug" or "D70" — the canonical
    writer canonicalizes filename stems before writing, and the model-level
    validator is the backstop.
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


class TestSourceCitationRoundTrip:
    """A free-text ``Source: file:line`` citation inside the rationale.

    The Rapid Cited Seed records the provenance of a documented decision as a
    plain ``Source: <file>:<line>`` line in the rationale body. That line must
    survive ``format_decision`` -> ``parse_decision`` byte-for-byte and must
    never be mistaken for the ``## Rejected Alternatives`` anchor (the
    rationale/rejected split keys on a whole-line ``## Rejected Alternatives``
    heading, not on the word "Rejected" appearing in prose).
    """

    def test_citation_line_survives_round_trip(self) -> None:
        rationale = (
            "Chose a store-owned daemon as the single shared store writer.\n\n"
            "Source: docs/adr/0003-shared-store-daemon.md:26"
        )
        decision = _minimal_decision(rationale=rationale)

        formatted = format_decision(decision)
        reparsed = parse_decision(formatted, "002-shared-store-daemon.md")

        assert reparsed.rationale == rationale
        assert "Source: docs/adr/0003-shared-store-daemon.md:26" in reparsed.rationale
        # No rejected alternatives were declared, so the citation prose did not
        # spuriously open a Rejected Alternatives section.
        assert reparsed.rejected == []

    def test_citation_does_not_break_rejected_anchor(self) -> None:
        """A citation line sitting just above a real Rejected Alternatives
        block must stay in the rationale; the anchor split is unaffected."""
        rationale = (
            "Daemon owns the shared store.\n\nSource: docs/adr/0003-shared-store-daemon.md:26"
        )
        decision = _minimal_decision(
            rationale=rationale,
            rejected=[
                RejectedAlternative(
                    name="Keep CLI-Only Embedded Access",
                    reason="No central place for request ordering or admission control.",
                ),
            ],
        )

        formatted = format_decision(decision)
        reparsed = parse_decision(formatted, "002-shared-store-daemon.md")

        # The citation rides with the rationale, not the rejected section.
        assert reparsed.rationale == rationale
        assert "Source: docs/adr" in reparsed.rationale
        assert [r.name for r in reparsed.rejected] == ["Keep CLI-Only Embedded Access"]
        assert "Source:" not in (reparsed.rejected[0].reason or "")

    def test_citation_with_literal_colon_line_byte_identical(self) -> None:
        """Reformat is idempotent with the citation present (byte-identical)."""
        rationale = "Pinned the store path outside the repo.\n\nSource: CLAUDE.md:12"
        decision = _minimal_decision(num=2, rationale=rationale)
        once = format_decision(decision)
        twice = format_decision(parse_decision(once, "002-store-path.md"))
        assert once == twice


# ── Frontmatter split helper ──


class TestSplitFrontmatter:
    """Direct unit tests for ``_split_frontmatter``.

    The split must stay byte-identical to the historical inline slice, which
    used the literal offsets 4 (length of the open fence) and 5 (length of the
    close fence).
    """

    def test_valid_input_byte_exact_against_literal_slice(self) -> None:
        text = (
            "---\n"
            "date: 2026-04-01\n"
            "confidence: high\n"
            "---\n\n"
            "# 001 — Title\n\n"
            "## Decision\n\nChose A.\n"
        )
        # Reproduce the pre-refactor inline slice exactly.
        fm_end = text.find("\n---\n", 4)
        expected_block = text[4:fm_end]
        expected_body = text[fm_end + 5 :]

        block, body = _split_frontmatter(text, "001-title.md")
        assert block == expected_block
        assert body == expected_body

    def test_empty_frontmatter_yields_empty_block(self) -> None:
        # An open fence immediately followed by the close fence: the block and
        # the body are both empty. ("---\n\n---\n" is the smallest input whose
        # close fence has the leading newline the fence requires.)
        block, body = _split_frontmatter("---\n\n---\n", "001-empty.md")
        assert block == ""
        assert body == ""

    def test_missing_open_fence_raises(self) -> None:
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            _split_frontmatter("no fence here\n", "001-no-fence.md")

    def test_unterminated_raises(self) -> None:
        with pytest.raises(ValueError, match="unterminated"):
            _split_frontmatter("---\ndate: 2026-04-01\n", "001-unterminated.md")


# ── Enum-value byte pins ──


class TestEnumValueBytes:
    """Lock the serialized bytes of the enum members used as fallback literals.

    Fallback defaults across the operations layer reference these members'
    ``.value`` instead of bare string literals. The bytes must stay exact so a
    future member rename fails here loudly instead of silently changing the
    serialized status/confidence tokens on disk.
    """

    def test_status_active_value(self) -> None:
        assert DecisionStatus.active.value == "active"

    def test_confidence_medium_value(self) -> None:
        assert DecisionConfidence.medium.value == "medium"
