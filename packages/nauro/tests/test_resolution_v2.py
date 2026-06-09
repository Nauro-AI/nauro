"""Tests for the v2 project-resolution flow used by every CLI command.

The CLI's ``resolve_target_project`` and the local MCP servers'
``_resolve_store`` share the same priority order: explicit ``--project``
flag → cwd ``.nauro/config.json`` walk-up → v1 legacy fallback. These
tests pin that behavior end-to-end so a regression cannot silently flip
the precedence.

Also covers the integration scenario where two projects coexist (one
cloud-style preconfigured, one freshly created via `nauro init`) and
each cwd resolves to its own store.
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.cli.utils import resolve_target_project
from nauro.constants import (
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION_V2,
    REPO_CONFIG_DIR,
    REPO_CONFIG_FILENAME,
    REPO_CONFIG_SCHEMA_VERSION,
)
from nauro.mcp.stdio_server import _resolve_store
from nauro.store import registry
from nauro.store.repo_config import load_repo_config, save_repo_config

runner = CliRunner()

# Two distinct corrupt-config shapes that must reach the same graceful outcome:
# (a) unparseable JSON, which the reader now remaps to RepoConfigSchemaError, and
# (b) parseable JSON whose schema is wrong, which already raised that error.
_CORRUPT_CONFIGS = {
    "invalid_json": '{"mode": "local", "id": "01',  # truncated mid-value
    "wrong_schema": json.dumps({"mode": "local", "id": "x", "schema_version": 99}),
}


def _write_repo_config_text(repo_root, text: str) -> None:
    """Write raw config text under ``<repo_root>/.nauro/config.json``.

    Bypasses ``save_repo_config`` so the on-disk bytes can be corrupt — the
    writer validates before write and would refuse these shapes.
    """
    cfg_dir = repo_root / REPO_CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / REPO_CONFIG_FILENAME).write_text(text)


def _post_migration_state(tmp_path, monkeypatch):
    """Simulate the post-manual-migration state: v2 registry + cloud repo config."""
    cloud_pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    repo_root = tmp_path / "cloud_repo"
    repo_root.mkdir()
    (tmp_path / "projects" / cloud_pid).mkdir(parents=True)
    (tmp_path / REGISTRY_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION_V2,
                "projects": {
                    cloud_pid: {
                        "name": "nauro",
                        "mode": "cloud",
                        "server_url": "https://mcp.nauro.ai",
                        "repo_paths": [str(repo_root.resolve())],
                    }
                },
            }
        )
        + "\n"
    )
    save_repo_config(
        repo_root,
        {
            "mode": "cloud",
            "id": cloud_pid,
            "name": "nauro",
            "server_url": "https://mcp.nauro.ai",
        },
    )
    return cloud_pid, repo_root


# ── repo config wins over cwd-only registry lookup ───────────────────────────


def test_repo_config_resolves_to_id_keyed_store(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    name, store = resolve_target_project(None)
    assert name == "nauro"
    assert store == tmp_path / "projects" / cloud_pid


def test_resolution_walks_up_from_nested_dir(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    nested = repo_root / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    _name, store = resolve_target_project(None)
    assert store == tmp_path / "projects" / cloud_pid


def test_explicit_project_flag_overrides_repo_config(tmp_path, monkeypatch):
    """`--project <other-name>` takes precedence over the cwd config.

    Existing test_cli.py expectation; pinned here against the v2 path.
    """
    _cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    other_pid, _store = registry.register_project_v2(
        "beta",
        [tmp_path / "beta_repo"],
        mode="local",
    )
    (tmp_path / "beta_repo").mkdir()
    monkeypatch.chdir(repo_root)
    name, store = resolve_target_project("beta")
    assert name == "beta"
    assert store == tmp_path / "projects" / other_pid


def test_duplicate_name_disambiguation_shows_full_ulid(tmp_path, monkeypatch, capsys):
    """Two projects sharing a name must disambiguate by FULL ULID. ULIDs minted
    seconds apart share a long prefix, so a short slice can render identically
    for both matches and is not accepted as a ``--project`` value anyway."""
    pid_a, _ = registry.register_project_v2("dup", [tmp_path / "a"], mode="local")
    pid_b, _ = registry.register_project_v2("dup", [tmp_path / "b"], mode="local")
    assert pid_a != pid_b

    with pytest.raises(typer.Exit) as exc:
        resolve_target_project("dup")
    assert exc.value.exit_code == 1

    err = capsys.readouterr().err
    assert "Multiple projects named 'dup'" in err
    # Both full ULIDs (the only values the resolver accepts) appear verbatim.
    assert pid_a in err
    assert pid_b in err


# ── stdio _resolve_store mirrors the same precedence ─────────────────────────


def test_stdio_resolve_uses_repo_config_when_no_project_passed(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    store = _resolve_store(None, None)
    assert store == tmp_path / "projects" / cloud_pid


def test_stdio_resolve_mismatched_project_id_errors(tmp_path, monkeypatch):
    """When supplied project_id != repo config id, raise rather than silently swap."""
    _cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    with pytest.raises(ValueError, match="does not match"):
        _resolve_store("01KZZZZZZZZZZZZZZZZZZZZZZZ", str(repo_root))


def test_stdio_resolve_no_repo_no_project_errors(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)
    # Typed NoProjectError carries the welcome anchor.
    from nauro.store.resolution import NoProjectError

    with pytest.raises(NoProjectError, match="No Nauro project found"):
        _resolve_store(None, None)


def test_stdio_resolve_explicit_id_matching_config(tmp_path, monkeypatch):
    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)
    monkeypatch.chdir(repo_root)
    store = _resolve_store(cloud_pid, str(repo_root))
    assert store == tmp_path / "projects" / cloud_pid


# ── corrupt repo config degrades gracefully (no transport crash) ─────────────


@pytest.mark.parametrize("corrupt_kind", sorted(_CORRUPT_CONFIGS))
def test_resolve_via_repo_config_returns_none_on_corrupt_config(
    tmp_path, monkeypatch, corrupt_kind
):
    """Both corrupt shapes resolve to None rather than raising.

    Resolution runs on the MCP transport path, so a raised JSONDecodeError
    would crash the transport. A wrong-schema config already degraded; this
    pins that a truncated/unparseable config reaches the same fallback.
    """
    from nauro.store.resolution import resolve_via_repo_config

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config_text(repo_root, _CORRUPT_CONFIGS[corrupt_kind])
    monkeypatch.chdir(repo_root)

    assert resolve_via_repo_config(repo_root) is None


def test_resolve_via_repo_config_returns_none_on_traversal_id(tmp_path, monkeypatch):
    """End-to-end closure of the trust-boundary bug: a cloned repo whose
    ``.nauro/config.json`` carries a path-traversal ``id`` resolves to
    no-project rather than relocating the store outside ~/.nauro/projects/.

    Without the ULID guard the malformed (but otherwise schema-valid) id would
    flow through ``get_store_path_v2`` into a store path under an attacker-chosen
    directory, letting get_raw_file / propose_decision reach arbitrary local
    files when an agent merely opens the repo. The resolver must instead degrade
    to the no-project fallback.
    """
    from nauro.store.resolution import resolve_via_repo_config

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config_text(
        repo_root,
        json.dumps(
            {
                "mode": "local",
                "id": "../../../../../../etc",
                "name": "evil",
                "schema_version": REPO_CONFIG_SCHEMA_VERSION,
            }
        ),
    )
    monkeypatch.chdir(repo_root)

    assert resolve_via_repo_config(repo_root) is None


@pytest.mark.parametrize("corrupt_kind", sorted(_CORRUPT_CONFIGS))
def test_stdio_resolve_corrupt_config_raises_no_project(tmp_path, monkeypatch, corrupt_kind):
    """A corrupt config falls through to the welcome anchor, not a crash.

    Both corrupt shapes reach the identical NoProjectError outcome — the
    genuine no-project case the transport surfaces as the welcome screen.
    """
    from nauro.store.resolution import NoProjectError

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config_text(repo_root, _CORRUPT_CONFIGS[corrupt_kind])
    monkeypatch.chdir(repo_root)

    with pytest.raises(NoProjectError, match="No Nauro project found"):
        _resolve_store(None, None)


def test_get_context_does_not_crash_transport_on_corrupt_config(tmp_path, monkeypatch):
    """End-to-end: the get_context MCP wrapper returns rather than raising when
    the repo config is corrupt.

    The original bug was a transport crash — a corrupt config let a parse error
    escape the resolver and propagate out of the tool. This pins the wrapper's
    try/except that converts the resolution failure into a returned
    CallToolResult, coverage the two parametrized tests above do not reach
    because they stop at _resolve_store raising.
    """
    from mcp.types import CallToolResult

    from nauro.mcp.stdio_server import get_context

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config_text(repo_root, _CORRUPT_CONFIGS["invalid_json"])
    monkeypatch.chdir(repo_root)

    result = get_context(project_id=None, cwd=None)
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1


# ── integration: two projects coexist post-migration ─────────────────────────


def test_two_projects_each_resolve_to_own_store(tmp_path, monkeypatch):
    """Pre-migrated cloud project + a freshly-created local project don't collide."""
    cloud_pid, cloud_repo = _post_migration_state(tmp_path, monkeypatch)

    other = tmp_path / "other_repo"
    other.mkdir()
    monkeypatch.chdir(other)
    result = runner.invoke(app, ["init", "side"])
    assert result.exit_code == 0, result.output

    matches = registry.find_projects_by_name_v2("side")
    assert len(matches) == 1
    side_pid, _entry = matches[0]
    assert side_pid != cloud_pid

    # cwd in cloud repo → cloud store
    monkeypatch.chdir(cloud_repo)
    _, store = resolve_target_project(None)
    assert store == tmp_path / "projects" / cloud_pid

    # cwd in other repo → side store
    monkeypatch.chdir(other)
    _, store = resolve_target_project(None)
    assert store == tmp_path / "projects" / side_pid

    # Cloud-mode config not corrupted by the new local-mode init in a different dir
    cfg = load_repo_config(cloud_repo)
    assert cfg["mode"] == "cloud"
    assert cfg["id"] == cloud_pid


# ── available-project listing excludes blank tokens ──────────────────────────
#
# The listing unions v1 names (registry ``projects`` keys) with v2 names
# (``name`` field of each v2 entry). A blank token can enter from either side:
# a v2 entry missing ``name`` resolves to "", and a malformed v1 entry can have
# a "" key. The old ``set(v1) | set(v2) - {""}`` grouping only stripped the
# blank from the v2 operand (``-`` binds tighter than ``|``), so a blank on the
# v1 side leaked into "Available projects:". These tests drive a blank in on
# the v1 side so they fail against the old grouping and pass against the fix.


def _write_registry(tmp_path, schema_version: int, projects: dict) -> None:
    """Write registry.json with the given schema version and ``projects`` map."""
    (tmp_path / REGISTRY_FILENAME).write_text(
        json.dumps({"schema_version": schema_version, "projects": projects}) + "\n"
    )


def test_available_project_names_excludes_blank_from_combined_set(tmp_path, monkeypatch):
    """A blank entry on the v1 side must not leak into the available list.

    Pins the operator-precedence fix: the blank is subtracted from the
    *combined* v1 ∪ v2 set, not just the v2 operand.
    """
    from nauro.cli.utils import _available_project_names

    _write_registry(
        tmp_path,
        1,
        {
            "alpha": {"repo_paths": []},
            "": {"repo_paths": []},  # malformed blank-named v1 entry
        },
    )
    monkeypatch.chdir(tmp_path)

    names = _available_project_names()
    assert "" not in names
    assert names == ["alpha"]


def test_unknown_project_listing_has_no_blank_token(tmp_path, monkeypatch, capsys):
    """The ``--project`` error path lists only real names, never a blank.

    A blank-named entry previously leaked an empty token into
    "Available projects:" because ``-`` bound tighter than ``|``.
    """
    _write_registry(
        tmp_path,
        1,
        {
            "alpha": {"repo_paths": []},
            "": {"repo_paths": []},  # malformed blank-named v1 entry
        },
    )
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    with pytest.raises(typer.Exit) as excinfo:
        resolve_target_project("does-not-exist")
    assert excinfo.value.exit_code == 1

    err = capsys.readouterr().err
    listing_line = next(line for line in err.splitlines() if line.startswith("Available projects:"))
    parsed = [name.strip() for name in listing_line[len("Available projects:") :].split(",")]
    assert parsed == ["alpha"]
    assert "" not in parsed


# ── _resolve_project_entry: v2-first, v1-fallback, repo_paths guard ──────────


def test_resolve_project_entry_v2_first(tmp_path, monkeypatch):
    """A v2 entry is resolved by its id-keyed project_key."""
    from nauro.cli.utils import _resolve_project_entry

    repo = tmp_path / "repo"
    repo.mkdir()
    pid, _store = registry.register_project_v2("alpha", [repo])

    entry = _resolve_project_entry("alpha", pid)

    assert entry == registry.get_project_v2(pid)
    assert entry["repo_paths"] == [str(repo.resolve())]


def test_resolve_project_entry_v1_fallback(tmp_path, monkeypatch):
    """A v1 (name-keyed legacy) entry is resolved when no v2 entry matches."""
    from nauro.cli.utils import _resolve_project_entry

    repo = tmp_path / "repo"
    repo.mkdir()
    registry.register_project("beta", [repo])

    # The v2 lookup misses (no id-keyed entry); the name-keyed v1 entry wins.
    entry = _resolve_project_entry("beta", "01MISSINGV2KEY0000000000")

    assert entry == registry.get_project("beta")
    assert entry["repo_paths"] == [str(repo.resolve())]


def test_resolve_project_entry_no_repos_exits(tmp_path, monkeypatch, capsys):
    """An entry with no repo_paths exits 1 with the exact message on stderr."""
    from nauro.cli.utils import _resolve_project_entry

    _write_registry(tmp_path, 1, {"gamma": {"repo_paths": []}})

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_project_entry("gamma", "gamma")
    assert excinfo.value.exit_code == 1

    err = capsys.readouterr().err
    assert "Project 'gamma' has no associated repos." in err
