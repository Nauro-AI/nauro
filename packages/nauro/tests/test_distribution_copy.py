"""Semantic guards for repository and package distribution copy."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[3]


def test_readme_uses_stable_context_and_setup_claims() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "L0 for a concise orientation" in readme
    assert "L1 for a bounded working set" in readme
    assert "L2 for a full dump" in readme
    assert "hundreds of thousands of tokens" in readme
    assert "nauro setup all --with-subagents" in readme
    assert "tests across" not in readme
    assert "o200k_base" not in readme


def test_package_and_registry_descriptions_name_human_authority() -> None:
    pyproject = tomllib.loads((ROOT / "packages/nauro/pyproject.toml").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert "Human-ratified project judgement" in pyproject["project"]["description"]
    assert "Human-ratified project judgment" in server["description"]
    assert "approved corrections" in server["description"]


def test_contributor_catalogs_describe_retrieval_without_judgment() -> None:
    for relative in ("CLAUDE.md", "packages/nauro/CLAUDE.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "surface related decisions without writing" in text
        assert "check for conflicts without writing" not in text
