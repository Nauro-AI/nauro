"""Read results are discarded when generation authority appears during rendering."""

import pytest
from typer.testing import CliRunner

from nauro.cli import autogen
from nauro.cli.main import app
from nauro.mcp import read_dispatch, stdio_server
from nauro.store.registry import register_project_v2


@pytest.mark.parametrize(
    "surface,phase",
    [
        ("stdio", "adapter"),
        ("stdio", "renderer"),
        ("cli-json", "adapter"),
        ("cli-text", "adapter"),
        ("cli-text", "renderer"),
    ],
)
def test_cutover_discards_legacy_content(tmp_path, monkeypatch, surface, phase):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project, store = register_project_v2("Cutover", [])
    store.mkdir(parents=True, exist_ok=True)
    (store / "state.md").write_text("SENSITIVE OLD CONTENT")

    def cutover():
        root = store / ".replica"
        root.mkdir()
        (root / "authority.json").write_text("INCOMPLETE CUTOVER")

    if phase == "adapter":
        original = read_dispatch.legacy.tool_get_raw_file

        def read(*args, **kwargs):
            value = original(*args, **kwargs)
            cutover()
            return value

        monkeypatch.setattr(read_dispatch.legacy, "tool_get_raw_file", read)
    else:
        module = read_dispatch if surface == "stdio" else autogen
        original = module.try_render_envelope

        def render(*args, **kwargs):
            value = original(*args, **kwargs)
            cutover()
            return value

        monkeypatch.setattr(module, "try_render_envelope", render)
    if surface == "stdio":
        result = stdio_server.get_raw_file("state.md", project_id=project)
        assert result.isError is True
        assert "SENSITIVE OLD CONTENT" not in str(result)
    else:
        result = CliRunner().invoke(
            app, ["get-raw-file", "state.md", "--project", "Cutover", "--format", surface[4:]]
        )
        assert result.exit_code == 1, result.output
        assert result.stdout == ""
        assert "SENSITIVE OLD CONTENT" not in result.output
