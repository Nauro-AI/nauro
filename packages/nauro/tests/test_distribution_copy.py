"""Semantic guards for repository and package distribution copy."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[3]

TAGLINE = "What every agent should know"

SUPPORT_LINE = "Keep your project's direction in human hands as agents do more of the work."

FIT_BOUNDARY = (
    "If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, "
    "Nauro may be more than you need."
)

COMPACT_DESCRIPTION = (
    "What every agent should know. Keep your project's direction in human hands as "
    "agents do more of the work."
)

README_PATHS = ("README.md", "packages/nauro/README.md")

BUNDLED_SKILL_NAMES = (
    "nauro-adopt",
    "nauro-ship-task",
    "nauro-context",
    "nauro-loop",
    "nauro-interview",
)

PUBLIC_COPY_PATHS = (
    *README_PATHS,
    "packages/nauro/pyproject.toml",
    "server.json",
    "packages/nauro/src/nauro/cli/main.py",
)


def test_root_readme_stays_within_first_use_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 400 <= len(readme.split()) <= 520
    assert "uv tool install nauro" in readme
    assert "nauro init --demo" in readme
    assert (
        'nauro check-decision "Replace envelope budgeting '
        'with a passive spending tracker and charts"'
        in readme
    )
    assert "Envelope budgeting method" in readme
    assert readme.index("## See it in practice") < readme.index("## Try the demo (optional)")
    assert readme.index("## Try the demo (optional)") < readme.index("Pennykeep")
    assert "nauro adopt" in readme
    assert "/nauro-adopt" in readme


def test_readmes_carry_tagline_support_line_and_fit_boundary() -> None:
    for relative in README_PATHS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert TAGLINE in text, relative
        assert SUPPORT_LINE in text, relative
        assert FIT_BOUNDARY in text, relative


def test_readmes_carry_cursor_and_skill_onboarding_contract() -> None:
    for relative in README_PATHS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "@nauro-adopt" in text, relative
        assert "nauro adopt --with-skills --with-subagents" in text, relative
        assert "/nauro-adopt" in text, relative
        assert "$nauro-adopt" in text, relative
        if relative == "README.md":
            assert "https://nauro.ai/docs/agents-and-skills" in text
            assert "https://nauro.ai/docs/quickstart" in text
            continue
        assert "nauro setup cursor" in text, relative
        assert ".cursor/mcp.json" in text, relative
        assert "cursor.com/agents" in text, relative
        assert ".cursor/agents/nauro-*.md" in text, relative
        assert "`nauro-loop` Program Delivery stays on hold" in text, relative
        for skill_name in BUNDLED_SKILL_NAMES:
            assert skill_name in text, (relative, skill_name)


def test_compact_description_contract_across_distribution_surfaces() -> None:
    pyproject = tomllib.loads((ROOT / "packages/nauro/pyproject.toml").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert pyproject["project"]["description"] == COMPACT_DESCRIPTION

    from nauro.cli.main import app

    assert app.info.help is not None
    assert app.info.help.startswith(COMPACT_DESCRIPTION)
    assert server["title"] == "Nauro"
    assert server["description"] == TAGLINE
    assert len(server["description"]) <= 100
    assert {
        surface
        for surface, description in {
            "pypi": pyproject["project"]["description"],
            "cli": app.info.help,
            "mcp_registry": server["description"],
        }.items()
        if description != COMPACT_DESCRIPTION
    } == {"mcp_registry"}


def test_public_copy_uses_us_judgment_spelling() -> None:
    for relative in PUBLIC_COPY_PATHS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "judgement" not in text.lower(), relative


def test_contributor_catalogs_describe_retrieval_without_judgment() -> None:
    for relative in ("CLAUDE.md", "packages/nauro/CLAUDE.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "surface related decisions without writing" in text
        assert "check for conflicts without writing" not in text
