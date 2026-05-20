"""Regression tests for AGENTS.md ``## Skills`` + ``# Manual`` preservation across regen.

Both sections are preserved verbatim across ``nauro sync`` regeneration; the
order in which they appear in the source AGENTS.md is preserved too.
"""

from __future__ import annotations

from pathlib import Path

from nauro.templates.agents_md import (
    generate_agents_md,
    parse_preserved_sections,
)

PROJECT = "testproj"
PAYLOAD = "L0 PAYLOAD"


def _full_agents_md(
    *, skills: str | None = None, manual: str | None = None, skills_first: bool = True
) -> str:
    """Build a synthetic AGENTS.md covering the four parse cases."""
    order = None
    if skills is not None and manual is not None:
        order = ["skills", "manual"] if skills_first else ["manual", "skills"]
    return generate_agents_md(
        PROJECT,
        PAYLOAD,
        manual_section=manual,
        skills_section=skills,
        section_order=order,
    )


def test_regen_preserves_only_manual_section_existing_case(tmp_path: Path):
    """AGENTS.md with only `# Manual` content keeps preserving as before."""
    agents_md = tmp_path / "AGENTS.md"
    initial = generate_agents_md(PROJECT, "v1 payload", manual_section="Use Conventional Commits.")
    agents_md.write_text(initial)

    parsed = parse_preserved_sections(agents_md)
    assert parsed.skills is None
    assert parsed.manual == "Use Conventional Commits."
    assert parsed.order == ["manual"]

    regenerated = generate_agents_md(
        PROJECT,
        "v2 payload",
        manual_section=parsed.manual,
        skills_section=parsed.skills,
        section_order=parsed.order,
    )
    assert "v2 payload" in regenerated
    assert "Use Conventional Commits." in regenerated
    # No `## Skills` block emitted when skills_section is None.
    assert "## Skills" not in regenerated


def test_regen_preserves_only_skills_section_new_case(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    initial = _full_agents_md(skills="Use `/nauro-adopt` to seed context.", manual=None)
    agents_md.write_text(initial)

    parsed = parse_preserved_sections(agents_md)
    assert parsed.skills == "Use `/nauro-adopt` to seed context."
    assert parsed.manual is None
    assert parsed.order == ["skills"]


def test_regen_preserves_both_sections_skills_first(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    initial = _full_agents_md(
        skills="The /nauro-adopt skill seeds context.",
        manual="Use Conventional Commits.",
        skills_first=True,
    )
    agents_md.write_text(initial)

    parsed = parse_preserved_sections(agents_md)
    assert parsed.skills == "The /nauro-adopt skill seeds context."
    assert parsed.manual == "Use Conventional Commits."
    assert parsed.order == ["skills", "manual"]

    regenerated = generate_agents_md(
        PROJECT,
        "fresh payload",
        manual_section=parsed.manual,
        skills_section=parsed.skills,
        section_order=parsed.order,
    )
    skills_pos = regenerated.find("## Skills")
    manual_pos = regenerated.find("# Manual")
    assert skills_pos != -1
    assert manual_pos != -1
    assert skills_pos < manual_pos


def test_regen_preserves_both_sections_manual_first(tmp_path: Path):
    """If the user had `# Manual` before `## Skills`, regen keeps that order."""
    agents_md = tmp_path / "AGENTS.md"
    initial = _full_agents_md(
        skills="Skill block.",
        manual="Manual block.",
        skills_first=False,
    )
    agents_md.write_text(initial)

    parsed = parse_preserved_sections(agents_md)
    assert parsed.order == ["manual", "skills"]

    regenerated = generate_agents_md(
        PROJECT,
        "fresh payload",
        manual_section=parsed.manual,
        skills_section=parsed.skills,
        section_order=parsed.order,
    )
    skills_pos = regenerated.find("## Skills")
    manual_pos = regenerated.find("# Manual")
    assert manual_pos < skills_pos


def test_regen_with_neither_section_writes_fresh(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# AGENTS.md\n\nSome other content with no Manual or Skills marker.\n")

    parsed = parse_preserved_sections(agents_md)
    assert parsed.skills is None
    assert parsed.manual is None
    assert parsed.order == []

    regenerated = generate_agents_md(PROJECT, "payload")
    # Default order emits `# Manual` (empty) but no `## Skills`.
    assert "# Manual" in regenerated
    assert "## Skills" not in regenerated


def test_regen_strips_stale_attribution_footer_from_manual_section(tmp_path: Path):
    """Regression: a legacy ``nauro.dev`` attribution footer that ended up
    embedded under ``# Manual`` (because the parser's footer marker was tied
    to the current canonical URL) must not survive regen.

    Before the fix, the parser preserved the stale ``nauro.dev`` footer as
    user content and the regenerator appended a fresh ``nauro.ai`` footer
    below it, producing a duplicate attribution at every sync.
    """
    stale = "*Generated by [Nauro](https://nauro.dev) — project context every connected AI agent inherits.*"  # noqa: E501
    current = "*Generated by [Nauro](https://nauro.ai) — project doctrine every connected AI agent inherits.*"  # noqa: E501
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        f"# AGENTS.md\n\n## Project: testproj\n\nL0\n\n# Manual\n\n---\n{stale}\n\n---\n{current}\n"
    )

    parsed = parse_preserved_sections(agents_md)
    assert parsed.manual is None
    assert parsed.skills is None

    regenerated = generate_agents_md(
        PROJECT,
        PAYLOAD,
        manual_section=parsed.manual,
        skills_section=parsed.skills,
        section_order=parsed.order or None,
    )
    assert regenerated.count("*Generated by [Nauro](") == 1
    assert "nauro.dev" not in regenerated


def test_regen_preserves_user_manual_above_stale_footer(tmp_path: Path):
    """User-authored manual content above a stale attribution footer must
    survive; only the auto-generated footer line is dropped."""
    stale = "*Generated by [Nauro](https://nauro.dev) — project context every connected AI agent inherits.*"  # noqa: E501
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(
        "# AGENTS.md\n\n"
        "## Project: testproj\n\nL0\n\n"
        "# Manual\n\n"
        f"Use Conventional Commits.\n\n---\n{stale}\n"
    )

    parsed = parse_preserved_sections(agents_md)
    assert parsed.manual == "Use Conventional Commits."

    regenerated = generate_agents_md(
        PROJECT,
        PAYLOAD,
        manual_section=parsed.manual,
        skills_section=parsed.skills,
        section_order=parsed.order or None,
    )
    assert regenerated.count("*Generated by [Nauro](") == 1
    assert "Use Conventional Commits." in regenerated


def test_regen_round_trip_preserves_user_edits_in_skills(tmp_path: Path):
    """User-edited `## Skills` content survives a regen cycle byte-for-byte."""
    agents_md = tmp_path / "AGENTS.md"
    skills_text = "Custom skill notes.\n\n- Bullet one\n- Bullet two"

    initial = generate_agents_md(
        PROJECT,
        "v1",
        skills_section=skills_text,
        manual_section="Manual.",
    )
    agents_md.write_text(initial)

    parsed_v1 = parse_preserved_sections(agents_md)
    regenerated = generate_agents_md(
        PROJECT,
        "v2",
        skills_section=parsed_v1.skills,
        manual_section=parsed_v1.manual,
        section_order=parsed_v1.order,
    )
    agents_md.write_text(regenerated)

    parsed_v2 = parse_preserved_sections(agents_md)
    assert parsed_v2.skills == parsed_v1.skills == skills_text
    assert parsed_v2.manual == parsed_v1.manual == "Manual."
