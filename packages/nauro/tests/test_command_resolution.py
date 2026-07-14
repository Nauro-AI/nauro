"""Tests for MCP/hook command resolution and validation.

Covers three units introduced to stop Nauro recording a fragile or dead nauro
path into its MCP and hook configs:

  - ``probe_nauro_command`` — the single subprocess seam (``nauro --version``).
  - ``_is_durable_install_path`` — the pipx/uv-tool vs project-venv heuristic.
  - ``_resolve_nauro_command`` — the resolver that prefers a validated durable
    install, diverts away from a dead/fragile venv, and warns loudly when only a
    fragile or unresolvable command exists.

The autouse conftest fixture stubs the probe and durability helpers so no other
test spawns a real binary. These tests capture the real implementations at import
(before that fixture patches) so they exercise the genuine logic; resolver tests
override the seam functions on their own monkeypatch instance.
"""

from __future__ import annotations

import subprocess

import pytest

from nauro.cli import utils as cli_utils
from nauro.cli.commands import setup as setup_mod

# Real implementations captured before the autouse fixture patches them.
_REAL_PROBE = cli_utils.probe_nauro_command
_REAL_DURABLE = cli_utils._is_durable_install_path


# ── probe_nauro_command ────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_probe_true_on_exit_zero(monkeypatch):
    monkeypatch.setattr(cli_utils.subprocess, "run", lambda *a, **k: _FakeProc(0))
    assert _REAL_PROBE("nauro") is True


def test_probe_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(cli_utils.subprocess, "run", lambda *a, **k: _FakeProc(1))
    assert _REAL_PROBE("nauro") is False


def test_probe_false_on_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("nauro")

    monkeypatch.setattr(cli_utils.subprocess, "run", boom)
    assert _REAL_PROBE("/gone/nauro") is False


def test_probe_false_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="nauro", timeout=1.5)

    monkeypatch.setattr(cli_utils.subprocess, "run", boom)
    assert _REAL_PROBE("nauro") is False


def test_probe_invokes_version_subcommand(monkeypatch):
    seen: dict = {}

    def capture(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeProc(0)

    monkeypatch.setattr(cli_utils.subprocess, "run", capture)
    _REAL_PROBE("/opt/nauro")
    assert seen["cmd"] == ["/opt/nauro", "--version"]


def test_probe_accepts_a_specific_subcommand(monkeypatch):
    seen: dict = {}

    def capture(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeProc(0)

    monkeypatch.setattr(cli_utils.subprocess, "run", capture)
    result = _REAL_PROBE("/opt/nauro", args=("hook", "codex-bootstrap", "--help"))

    assert result is True
    assert seen["cmd"] == ["/opt/nauro", "hook", "codex-bootstrap", "--help"]


# ── _is_durable_install_path ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        # pipx / uv-tool layouts are durable.
        ("/home/u/.local/pipx/venvs/nauro/bin/nauro", True),
        ("/home/u/.local/share/uv/tools/nauro/bin/nauro", True),
        # Project-local virtualenvs are fragile (grandparent is the venv dir).
        ("/repo/.venv/bin/nauro", False),
        ("/repo/venv/bin/nauro", False),
        ("/repo/env/bin/nauro", False),
        # System / Homebrew / conda: unknown shape defaults to durable.
        ("/usr/local/bin/nauro", True),
        ("/opt/homebrew/bin/nauro", True),
        ("/home/u/miniconda3/envs/proj/bin/nauro", True),
        # Windows Scripts layout (forward slashes so Path.parts splits on POSIX
        # test hosts): the .venv grandparent still marks it fragile, pipx durable.
        ("/c/Users/me/project/.venv/Scripts/nauro.exe", False),
        ("/c/Users/me/pipx/venvs/nauro/Scripts/nauro.exe", True),
    ],
)
def test_is_durable_install_path(path, expected):
    assert _REAL_DURABLE(path) is expected


# ── _resolve_nauro_command ─────────────────────────────────────────────────────


def _wire_resolver(monkeypatch, *, sibling, which, probe, durable):
    """Point the resolver at controlled candidates and seam functions."""
    monkeypatch.setattr(setup_mod, "_interpreter_sibling_candidate", lambda: sibling)
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: which)
    monkeypatch.setattr(cli_utils, "probe_nauro_command", probe)
    monkeypatch.setattr(cli_utils, "_is_durable_install_path", durable)


def test_resolver_records_durable_sibling_without_warning(monkeypatch, capsys):
    """Fast path: a sibling that runs and looks durable is recorded, no warning.

    This is the pipx/uv-tool/desktop flow — must stay byte-identical to before.
    """
    sibling = "/opt/pipx/venvs/nauro/bin/nauro"
    _wire_resolver(
        monkeypatch,
        sibling=sibling,
        which="/usr/local/bin/nauro",
        probe=lambda cmd, **k: True,
        durable=lambda p: True,
    )
    assert setup_mod._resolve_nauro_command() == sibling
    assert capsys.readouterr().err == ""


def test_resolver_diverts_to_which_when_sibling_dead(monkeypatch, capsys):
    """A dead fragile project-venv sibling diverts to a healthy durable PATH shim."""
    sibling = "/repo/.venv/bin/nauro"
    which = "/opt/pipx/venvs/nauro/bin/nauro"
    _wire_resolver(
        monkeypatch,
        sibling=sibling,
        which=which,
        probe=lambda cmd, **k: cmd == which,  # sibling crashes on import
        durable=lambda p: p == which,  # sibling is fragile, which is durable
    )
    assert setup_mod._resolve_nauro_command() == which
    # Silent divert — no warning when a durable replacement is found.
    assert capsys.readouterr().err == ""


def test_resolver_records_fragile_sibling_with_warning(monkeypatch, capsys):
    """Only a fragile-but-working sibling exists → record it, but warn loudly."""
    sibling = "/repo/.venv/bin/nauro"
    _wire_resolver(
        monkeypatch,
        sibling=sibling,
        which=None,  # nothing durable on PATH
        probe=lambda cmd, **k: True,  # sibling runs
        durable=lambda p: False,  # ...but is fragile
    )
    assert setup_mod._resolve_nauro_command() == sibling
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "virtualenv" in err
    assert sibling in err


def test_resolver_falls_back_with_loud_warning_when_nothing_runs(monkeypatch, capsys):
    """Nothing validates → keep the best absolute path and warn MCP won't work."""
    sibling = "/repo/.venv/bin/nauro"
    _wire_resolver(
        monkeypatch,
        sibling=sibling,
        which=None,
        probe=lambda cmd, **k: False,  # nothing runs
        durable=lambda p: False,
    )
    assert setup_mod._resolve_nauro_command() == sibling
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "will not work" in err


def test_resolver_bare_nauro_only_as_last_resort(monkeypatch, capsys):
    """Bare ``nauro`` is recorded only when no absolute path exists at all."""
    _wire_resolver(
        monkeypatch,
        sibling=None,
        which=None,
        probe=lambda cmd, **k: False,
        durable=lambda p: False,
    )
    assert setup_mod._resolve_nauro_command() == "nauro"
    assert "WARNING" in capsys.readouterr().err


def test_resolver_never_prefers_bare_over_absolute(monkeypatch, capsys):
    """Guarantee: an available absolute path always beats bare ``nauro``."""
    sibling = "/repo/.venv/bin/nauro"
    _wire_resolver(
        monkeypatch,
        sibling=sibling,
        which="/usr/local/bin/nauro",
        probe=lambda cmd, **k: False,  # neither validates
        durable=lambda p: False,
    )
    result = setup_mod._resolve_nauro_command()
    assert result != "nauro"
    assert result == sibling  # best absolute path retained
    capsys.readouterr()


# ── memoization ────────────────────────────────────────────────────────────────


def test_find_nauro_command_memoizes_resolution(monkeypatch):
    """The cached entrypoint resolves once; a second call spawns no new probe."""
    calls: list[str] = []

    def counting_probe(cmd, **kwargs):
        calls.append(cmd)
        return True

    monkeypatch.setattr(
        setup_mod, "_interpreter_sibling_candidate", lambda: "/opt/pipx/venvs/nauro/bin/nauro"
    )
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_utils, "probe_nauro_command", counting_probe)
    monkeypatch.setattr(cli_utils, "_is_durable_install_path", lambda p: True)
    setup_mod._find_nauro_command.cache_clear()

    first = setup_mod._find_nauro_command()
    after_first = len(calls)
    second = setup_mod._find_nauro_command()

    assert first == second == "/opt/pipx/venvs/nauro/bin/nauro"
    assert len(calls) == after_first  # no re-probe on the cached call


def test_setup_all_resolves_command_once(tmp_path, monkeypatch):
    """`setup all` validates the entrypoint once across all five sinks."""
    from nauro.cli.commands.setup import setup_all_surfaces

    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    calls: list[str] = []
    monkeypatch.setattr(
        setup_mod, "_interpreter_sibling_candidate", lambda: "/opt/pipx/venvs/nauro/bin/nauro"
    )
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli_utils, "probe_nauro_command", lambda cmd, **k: calls.append(cmd) or True
    )
    monkeypatch.setattr(cli_utils, "_is_durable_install_path", lambda p: True)
    setup_mod._find_nauro_command.cache_clear()

    setup_all_surfaces([repo1, repo2], remove=False)

    # Two repos × two JSON surfaces + codex would be five _find_nauro_command
    # calls; memoization collapses them to a single probe.
    assert len(calls) == 1
