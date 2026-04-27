"""`nauro serve` no longer accepts --project — one source of truth is the repo config.

Also asserts the s3_prefix shape so the migration's cwd-based prefix
construction lines up with the cloud Lambda's expectations.
"""

from __future__ import annotations

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.sync.config import s3_prefix

runner = CliRunner()


def test_serve_rejects_project_flag():
    """The --project flag was removed; passing it is a usage error."""
    result = runner.invoke(app, ["serve", "--project", "anything"])
    assert result.exit_code != 0
    output = result.output + (result.stderr or "")
    assert "--project" in output or "No such option" in output or "no such option" in output


def test_s3_prefix_shape_for_project_id():
    """s3_prefix renders the canonical users/<user>/projects/<id>/ shape."""
    assert (
        s3_prefix("user-abc", "01KQ6AZGNA0B3QBF67NBXP3S45")
        == "users/user-abc/projects/01KQ6AZGNA0B3QBF67NBXP3S45/"
    )
