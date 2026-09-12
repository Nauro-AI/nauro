"""Skill rendering dispatches on surface and skill name through tables, not chains."""

import pytest

from nauro.skills import SKILL_DESCRIPTIONS, render_skill

SURFACES = ("claude_code", "codex", "cursor")
CURSOR_REWORDED = {("cursor", "nauro-ship-task")}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("skill_name", sorted(SKILL_DESCRIPTIONS))
def test_every_surface_and_skill_renders_frontmatter_and_body(surface: str, skill_name: str):
    rendered = render_skill(surface, skill_name)
    assert rendered.startswith("---\n")
    assert "\ndescription: " in rendered
    described = SKILL_DESCRIPTIONS[skill_name] in rendered
    assert described is not ((surface, skill_name) in CURSOR_REWORDED)
    assert "<!-- surface:" not in rendered


def test_unknown_surface_is_rejected():
    with pytest.raises(ValueError, match="unknown surface"):
        render_skill("emacs", "nauro-adopt")


def test_unknown_skill_is_rejected():
    with pytest.raises(ValueError, match="unknown skill"):
        render_skill("claude_code", "nauro-teleport")
