"""Registry data-integrity tests for `nauro init`.

* init refuses to mint a second registry entry for a repo an existing
  project already claims (the duplicate-entry footgun), even under --force.
* register_project_v2 validates the project name before any registry write,
  so garbage names never leak a half-written entry.

CWD and NAURO_HOME are both isolated to tmp_path by autouse conftest
fixtures; tests that need a specific cwd override on the same monkeypatch.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry
from nauro.store.registry import find_projects_by_name_v2, register_project_v2

runner = CliRunner()


# ── duplicate-claim refusal ────────────────────────────────────────────────────


def test_init_force_refuses_already_claimed_repo(tmp_path, monkeypatch):
    """`init projF` → note → `init projF --force` must not mint a second entry.

    --force bypasses the cwd-config overwrite guard, but the repo is still
    claimed by the first projF; re-initializing would shadow that association
    with a duplicate registry entry. The refusal is independent of --force and
    exits 1. The earlier decision must survive in the original store.
    """
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["init", "projF"])
    assert first.exit_code == 0, first.output

    matches = find_projects_by_name_v2("projF")
    assert len(matches) == 1
    pid, _entry = matches[0]

    note_res = runner.invoke(app, ["note", "Chose X for Y reasons"])
    assert note_res.exit_code == 0, note_res.output
    decisions_dir = tmp_path / "projects" / pid / "decisions"
    seeded = sorted(decisions_dir.glob("*.md"))
    assert seeded, "the note should have written a decision file"

    forced = runner.invoke(app, ["init", "projF", "--force"])
    assert forced.exit_code == 1, forced.output

    # Still exactly one projF entry; the decision survived untouched.
    assert len(find_projects_by_name_v2("projF")) == 1
    assert sorted((tmp_path / "projects" / pid / "decisions").glob("*.md")) == seeded


# ── project-name validation ────────────────────────────────────────────────────

# Names the locked validation rules reject: empty / whitespace-only,
# over-length, path separators, the '..' traversal substring, or a
# non-printable character. A single-leading-dot name (".hidden") is NOT in
# this set — the store path is ULID-keyed, so a leading dot is not a
# traversal risk and the locked rule does not enumerate it. ".dotdot.." below
# stands in for the dot-prefixed traversal case the rules do catch.
_INVALID_NAMES = [
    "",
    "   ",
    "x" * 300,
    "../escape",
    "foo/bar",
    ".dotdot..",
    "ctrl\x07char",
]


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_init_rejects_invalid_name_without_leaking_entry(bad_name, tmp_path, monkeypatch):
    """Invalid names exit 1 and leave no registry entry behind."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", bad_name])
    assert result.exit_code == 1, result.output
    assert find_projects_by_name_v2(bad_name) == []
    assert find_projects_by_name_v2(bad_name.strip()) == []


def test_init_accepts_valid_name(tmp_path, monkeypatch):
    """A valid name still registers and exits 0."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "valid-name_1"])
    assert result.exit_code == 0, result.output
    assert len(find_projects_by_name_v2("valid-name_1")) == 1


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_register_project_v2_raises_on_invalid_name(bad_name, tmp_path, monkeypatch):
    """register_project_v2 raises ValueError before touching the registry."""
    with pytest.raises(ValueError):
        register_project_v2(bad_name, [tmp_path])
    # No entry leaked even on the raising path.
    assert registry.load_registry_v2()["projects"] == {}
