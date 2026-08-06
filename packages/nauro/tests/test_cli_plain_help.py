"""Help output renders as plain Click text, not Rich panels.

``rich_markup_mode=None`` on the root app makes every ``--help`` surface —
the root, hand-written sub-apps, and the auto-generated tool mirrors —
render the classic ``Usage:``-prefixed layout without box-drawing
characters. Agent consumers parse that layout far more cheaply than the
Rich panel output.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app

runner = CliRunner()

# The Rich help layout's frame and rule characters. None of them may
# appear in plain help output.
_BOX_DRAWING_CHARS = "╭╮╰╯│─━┏┓┗┛┃═"


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["projects", "--help"],
        ["get-context", "--help"],
    ],
    ids=["root", "sub-app", "autogen"],
)
def test_help_is_plain_text(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Usage:")
    present = [ch for ch in _BOX_DRAWING_CHARS if ch in result.output]
    assert not present, f"Rich box-drawing characters in help output: {present}"
