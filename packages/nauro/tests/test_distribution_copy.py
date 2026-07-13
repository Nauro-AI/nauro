"""Semantic guards for repository and package distribution copy."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[3]

HEADLINE = "Give your agents the context code leaves out."

SUPPORT_LINE = (
    "Nauro keeps current state, open questions, and human-approved project judgment "
    "in one record, ready for every agent you connect."
)

FIT_BOUNDARY = (
    "If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, "
    "Nauro may be more than you need."
)

COMPACT_DESCRIPTION = (
    "Human-approved project judgment and current state for connected AI agents, "
    "surfaced before work."
)

README_PATHS = ("README.md", "packages/nauro/README.md")

PUBLIC_COPY_PATHS = (
    *README_PATHS,
    "packages/nauro/pyproject.toml",
    "server.json",
    "packages/nauro/src/nauro/cli/main.py",
)


def test_readme_uses_stable_context_and_setup_claims() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "L0 for a concise orientation" in readme
    assert "L1 for a bounded working set" in readme
    assert "L2 for a full dump" in readme
    assert "hundreds of thousands of tokens" in readme
    assert "nauro setup all --with-subagents" in readme
    assert "tests across" not in readme
    assert "o200k_base" not in readme


def test_readmes_carry_headline_support_line_and_fit_boundary() -> None:
    for relative in README_PATHS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert HEADLINE in text, relative
        assert SUPPORT_LINE in text, relative
        assert FIT_BOUNDARY in text, relative


def test_compact_description_identical_across_distribution_surfaces() -> None:
    pyproject = tomllib.loads((ROOT / "packages/nauro/pyproject.toml").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert pyproject["project"]["description"] == COMPACT_DESCRIPTION
    assert server["description"] == COMPACT_DESCRIPTION

    from nauro.cli.main import app

    assert app.info.help is not None
    assert app.info.help.startswith(COMPACT_DESCRIPTION)


def test_public_copy_uses_us_judgment_spelling() -> None:
    for relative in PUBLIC_COPY_PATHS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "judgement" not in text.lower(), relative


def test_contributor_catalogs_describe_retrieval_without_judgment() -> None:
    for relative in ("CLAUDE.md", "packages/nauro/CLAUDE.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "surface related decisions without writing" in text
        assert "check for conflicts without writing" not in text
