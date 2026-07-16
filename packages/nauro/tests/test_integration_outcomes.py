"""Typed-outcome pipeline scaffolding: RawLine carrier and render dispatch."""

from __future__ import annotations

import dataclasses

import pytest

from nauro.cli.integrations.outcomes import RawLine
from nauro.cli.integrations.render import render


def test_render_rawline_returns_verbatim_text():
    assert render(RawLine("x")) == ["x"]


def test_rawline_is_frozen():
    line = RawLine("x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.text = "y"
