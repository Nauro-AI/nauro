"""Tests for the v2 project-resolution flow used by every CLI command.

The CLI's ``resolve_target_project`` and the local MCP servers'
``_resolve_store`` share the same priority order: explicit ``--project``
flag → cwd ``.nauro/config.json`` walk-up → registry matched by repo
path. These tests pin that behavior end-to-end so a regression cannot
silently flip the precedence.

Also covers the integration scenario where two projects coexist (one
cloud-style preconfigured, one freshly created via `nauro init`) and
each cwd resolves to its own store.
"""

from __future__ import annotations

import json
import os

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
from nauro.templates.scaffolds import scaffold_project_store

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
    scaffold_project_store("nauro", tmp_path / "projects" / cloud_pid)
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
    scaffold_project_store("beta", _store)
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


# ── resolve_from_cwd: the canonical cwd waterfall shared by every surface ─────


def test_resolve_from_cwd_repo_config_tier(tmp_path, monkeypatch):
    """Tier 1: a ``.nauro/config.json`` walk-up resolves to the id-keyed store."""
    from nauro.store.resolution import resolve_from_cwd

    cloud_pid, repo_root = _post_migration_state(tmp_path, monkeypatch)

    resolution = resolve_from_cwd(repo_root)

    assert resolution is not None
    assert resolution.store_path == tmp_path / "projects" / cloud_pid
    assert resolution.project_id == cloud_pid
    assert resolution.display_name == "nauro"


def test_valid_repo_config_without_registry_is_typed_first_connection(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.project_id == pid
    assert result.reason_code == "not_connected_on_this_machine"
    assert result.recovery_actions == ("locate", "continue")
    assert "has not been connected on this machine" in result.guidance
    assert "nauro link --cloud" in result.guidance
    assert "Welcome to Nauro" not in result.guidance


def test_cloud_first_connection_offers_eligible_restore(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    save_repo_config(
        repo,
        {
            "mode": "cloud",
            "id": pid,
            "name": "Pareto",
            "server_url": "https://mcp.nauro.ai",
        },
    )

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "not_connected_on_this_machine"
    assert result.recovery_actions == ("locate", "restore", "continue")
    assert result.guidance == (
        "This cloud project has not been connected on this machine. "
        "Run `nauro reconnect` to verify access and restore its latest synced record."
    )


def test_registered_missing_store_is_typed_prior_connection_loss(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {pid: {"name": "Pareto", "mode": "local", "repo_paths": [str(repo)]}},
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "connected_record_missing"
    assert "was connected on this machine" in result.guidance
    assert "Welcome to Nauro" not in result.guidance


def test_registered_invalid_external_store_is_typed_invalid(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    invalid = tmp_path / "external" / pid
    invalid.mkdir(parents=True)
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                    "store_path": str(invalid),
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "connected_record_invalid"
    assert "Run `nauro reconnect`" in result.guidance
    assert "doctor" not in result.guidance


def test_incomplete_default_store_resolves_tolerantly(tmp_path, monkeypatch):
    """The Nauro-managed default store keeps its pre-recovery tolerance.

    An existing but structurally incomplete default-home store (e.g. created
    empty by an older attach whose sync never completed) must resolve so
    downstream tools can degrade gracefully — not dead-end every command in a
    connected_record_invalid state whose recovery menu cannot restore. Strict
    structural validation applies only to externally mapped store paths.
    """
    from nauro.store.resolution import RepoResolution, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    registry.get_store_path_v2(pid).mkdir(parents=True)
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, RepoResolution)
    assert result.store_path == registry.get_store_path_v2(pid)


def test_default_store_with_symlinked_component_is_typed_invalid(tmp_path, monkeypatch):
    """Tolerance for the managed default path means components may be
    absent — never that a pre-planted symlink may redirect store reads or
    sync writes outside the store.
    """
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    store = registry.get_store_path_v2(pid)
    store.mkdir(parents=True)
    (store / "project.md").write_text("# Pareto\n")
    outside = tmp_path / "outside-decisions"
    outside.mkdir()
    (store / "decisions").symlink_to(outside, target_is_directory=True)
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "connected_record_invalid"


def test_v1_shaped_registry_resolves_to_no_project(tmp_path, monkeypatch):
    """A retired v1-shaped (name-keyed) registry.json resolves to nothing.

    v1 registry support was removed deliberately: the shape reads as an
    empty v2 registry, so a matching repo path falls through to the
    no-project outcome instead of resolving or crashing.
    """
    from nauro.store.resolution import resolve_from_cwd

    repo = tmp_path / "v1repo"
    repo.mkdir()
    _write_registry(tmp_path, 1, {"legacy": {"repo_paths": [str(repo.resolve())]}})

    assert resolve_from_cwd(repo) is None


def test_v1_shaped_registry_reaches_welcome_screen(tmp_path, monkeypatch):
    """The stdio transport surfaces a v1-shaped registry as the welcome
    screen (typed NoProjectError), never a crash."""
    from nauro.store.resolution import NoProjectError

    repo = tmp_path / "v1repo"
    repo.mkdir()
    _write_registry(tmp_path, 1, {"legacy": {"repo_paths": [str(repo.resolve())]}})
    monkeypatch.chdir(repo)

    with pytest.raises(NoProjectError, match="No Nauro project found"):
        _resolve_store(None, None)


@pytest.mark.parametrize("raw_store_path", [42, "", None])
def test_malformed_registered_store_path_is_typed_invalid(tmp_path, monkeypatch, raw_store_path):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                    "store_path": raw_store_path,
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "connected_record_invalid"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_default_store_preserves_symlinked_nauro_home(tmp_path, monkeypatch):
    from nauro.store.resolution import RepoResolution, resolve_from_cwd

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("NAURO_HOME", str(linked_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    store_path = registry.get_store_path_v2(pid)
    scaffold_project_store("Pareto", store_path)
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, RepoResolution)
    assert result.store_path == store_path


def test_conflicting_default_and_external_stores_is_typed_conflict(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProject, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    external = tmp_path / "external" / pid
    scaffold_project_store("Pareto", external)
    scaffold_project_store("Pareto", registry.get_store_path_v2(pid))
    registry.save_registry_v2(
        {
            "schema_version": 2,
            "projects": {
                pid: {
                    "name": "Pareto",
                    "mode": "local",
                    "repo_paths": [str(repo)],
                    "store_path": str(external),
                }
            },
        }
    )
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    result = resolve_from_cwd(repo)

    assert isinstance(result, DisconnectedProject)
    assert result.reason_code == "connected_binding_conflict"
    assert "will not choose or overwrite either" in result.guidance


def test_valid_external_store_resolves_through_registry_binding(tmp_path, monkeypatch):
    from nauro.store.recovery import bind_local_store
    from nauro.store.resolution import RepoResolution, resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    external = tmp_path / "external" / pid
    scaffold_project_store("Pareto", external)
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})
    bind_local_store(repo, external)

    result = resolve_from_cwd(repo)

    assert isinstance(result, RepoResolution)
    assert result.store_path == external


def test_resolve_store_raises_typed_disconnected_error(tmp_path, monkeypatch):
    from nauro.store.resolution import DisconnectedProjectError

    repo = tmp_path / "repo"
    repo.mkdir()
    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    save_repo_config(repo, {"mode": "local", "id": pid, "name": "Pareto"})

    with pytest.raises(DisconnectedProjectError) as exc:
        _resolve_store(None, repo)

    assert exc.value.state.reason_code == "not_connected_on_this_machine"


def test_resolve_from_cwd_v2_registry_by_path_tier(tmp_path, monkeypatch):
    """Tier 2: no repo config, but the cwd is a registered v2 repo path."""
    from nauro.store.resolution import resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store = registry.register_project_v2("byname", [repo])

    resolution = resolve_from_cwd(repo)

    assert resolution is not None
    assert resolution.store_path == store
    assert resolution.project_id == pid
    assert resolution.display_name == "byname"


def test_resolve_from_cwd_repo_config_precedes_v2_registry(tmp_path, monkeypatch):
    """Tier 1 wins over tier 2: the repo config id is honored even when the same
    cwd is also a registered v2 repo path pointing at a different project."""
    from nauro.store.resolution import resolve_from_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    # The cwd is registered by path under one project id ...
    pid_by_path, _store = registry.register_project_v2("bypath", [repo])
    # ... but its repo config names a different, config-only project id.
    pid_config, _store2 = registry.register_project_v2("byconfig", [tmp_path / "elsewhere"])
    save_repo_config(repo, {"mode": "local", "id": pid_config, "name": "byconfig"})

    resolution = resolve_from_cwd(repo)

    assert resolution is not None
    assert resolution.project_id == pid_config
    assert resolution.store_path == tmp_path / "projects" / pid_config
    assert pid_config != pid_by_path


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_resolve_from_cwd_declines_symlinked_repo_config(tmp_path, monkeypatch):
    """Tier 1 declines when config.json is a symlink, even one that points at
    a fully valid config: a cloned repo is untrusted content, and a planted
    link must not let attacker-chosen content select which project a command
    operates on."""
    from nauro.store.resolution import resolve_from_cwd

    victim = tmp_path / "victim"
    victim.mkdir()
    pid, _store = registry.register_project_v2("victim", [victim])
    save_repo_config(victim, {"mode": "local", "id": pid, "name": "victim"})

    attack = tmp_path / "attack"
    (attack / REPO_CONFIG_DIR).mkdir(parents=True)
    (attack / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME).symlink_to(
        victim / REPO_CONFIG_DIR / REPO_CONFIG_FILENAME
    )

    assert resolve_from_cwd(attack) is None


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_resolve_from_cwd_declines_symlinked_nauro_dir(tmp_path, monkeypatch):
    """A symlinked ``.nauro`` directory declines tier-1 resolution the same way
    as a symlinked config file: the walk covers directory components too."""
    from nauro.store.resolution import resolve_from_cwd

    victim = tmp_path / "victim"
    victim.mkdir()
    pid, _store = registry.register_project_v2("victim", [victim])
    save_repo_config(victim, {"mode": "local", "id": pid, "name": "victim"})

    attack = tmp_path / "attack"
    attack.mkdir()
    (attack / REPO_CONFIG_DIR).symlink_to(victim / REPO_CONFIG_DIR)

    assert resolve_from_cwd(attack) is None


def test_resolve_from_cwd_none_when_nothing_matches(tmp_path, monkeypatch):
    """No repo config and no registry path match: the waterfall returns None."""
    from nauro.store.resolution import resolve_from_cwd

    isolated = tmp_path / "isolated"
    isolated.mkdir()

    assert resolve_from_cwd(isolated) is None


def test_stdio_resolve_welcome_on_oserror_reading_repo_config(tmp_path, monkeypatch):
    """Fork 1a: a raw OSError while reading the repo config degrades to the
    welcome/no-project outcome rather than propagating out of the transport.

    Before the shared tier-1 helper caught OSError alongside
    RepoConfigSchemaError, an unreadable ``.nauro/config.json`` let the OSError
    escape ``_resolve_store`` and crash the tool call. Now it reaches the same
    NoProjectError the genuine no-project case surfaces as the welcome screen.
    """
    from nauro.store import resolution
    from nauro.store.resolution import NoProjectError

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    save_repo_config(
        repo_root,
        {"mode": "local", "id": "01KQ6AZGNA0B3QBF67NBXP3S45", "name": "proj"},
    )

    def _raise_oserror(_repo_root):
        raise OSError("simulated unreadable repo config")

    monkeypatch.setattr(resolution, "load_repo_config", _raise_oserror)
    monkeypatch.chdir(repo_root)

    with pytest.raises(NoProjectError, match="No Nauro project found"):
        _resolve_store(None, None)


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
# The listing collects the ``name`` field of each registry entry; an entry
# missing ``name`` resolves to "" and must not leak an empty token into
# "Available projects:".


def _write_registry(tmp_path, schema_version: int, projects: dict) -> None:
    """Write registry.json with the given schema version and ``projects`` map."""
    (tmp_path / REGISTRY_FILENAME).write_text(
        json.dumps({"schema_version": schema_version, "projects": projects}) + "\n"
    )


def test_available_project_names_excludes_blank(tmp_path, monkeypatch):
    """An entry missing its ``name`` field must not leak into the listing."""
    from nauro.cli.utils import _available_project_names

    _write_registry(
        tmp_path,
        2,
        {
            "01KQ6AZGNA0B3QBF67NBXP3S45": {"name": "alpha", "mode": "local", "repo_paths": []},
            # malformed entry with no name → blank token candidate
            "01KQ6AZGNA0B3QBF67NBXP3S46": {"mode": "local", "repo_paths": []},
        },
    )
    monkeypatch.chdir(tmp_path)

    names = _available_project_names()
    assert "" not in names
    assert names == ["alpha"]


def test_unknown_project_listing_has_no_blank_token(tmp_path, monkeypatch, capsys):
    """The ``--project`` error path lists only real names, never a blank."""
    _write_registry(
        tmp_path,
        2,
        {
            "01KQ6AZGNA0B3QBF67NBXP3S45": {"name": "alpha", "mode": "local", "repo_paths": []},
            # malformed entry with no name → blank token candidate
            "01KQ6AZGNA0B3QBF67NBXP3S46": {"mode": "local", "repo_paths": []},
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


# ── _resolve_project_entry: id-keyed lookup + repo_paths guard ────────────────


def test_resolve_project_entry_by_id(tmp_path, monkeypatch):
    """An entry is resolved by its id-keyed project_key."""
    from nauro.cli.utils import _resolve_project_entry

    repo = tmp_path / "repo"
    repo.mkdir()
    pid, _store = registry.register_project_v2("alpha", [repo])

    entry = _resolve_project_entry("alpha", pid)

    assert entry == registry.get_project_v2(pid)
    assert entry["repo_paths"] == [str(repo.resolve())]


def test_resolve_project_entry_v1_shape_does_not_resolve(tmp_path, monkeypatch, capsys):
    """A retired v1 name-keyed entry no longer resolves; the caller gets the
    no-repos exit instead of a legacy fallback."""
    from nauro.cli.utils import _resolve_project_entry

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_registry(tmp_path, 1, {"beta": {"repo_paths": [str(repo.resolve())]}})

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_project_entry("beta", "01MISSINGV2KEY0000000000")
    assert excinfo.value.exit_code == 1

    err = capsys.readouterr().err
    assert "Project 'beta' has no associated repos." in err


def test_resolve_project_entry_no_repos_exits(tmp_path, monkeypatch, capsys):
    """An entry with no repo_paths exits 1 with the exact message on stderr."""
    from nauro.cli.utils import _resolve_project_entry

    pid = "01KQ6AZGNA0B3QBF67NBXP3S45"
    _write_registry(tmp_path, 2, {pid: {"name": "gamma", "mode": "local", "repo_paths": []}})

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_project_entry("gamma", pid)
    assert excinfo.value.exit_code == 1

    err = capsys.readouterr().err
    assert "Project 'gamma' has no associated repos." in err
