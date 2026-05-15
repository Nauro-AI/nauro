"""Shared pytest configuration."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Chdir every test into tmp_path so CWD walk-up resolution doesn't leak.

    Several store/resolution paths walk up from `Path.cwd()` looking for
    ``.nauro/config.json``. If pytest is run from inside an adopted repo
    (e.g. the nauro repo dogfood-adopting itself), that walk finds a real
    config and trips ID-mismatch errors in tests that pass project_id= directly.
    Tests that need a specific CWD use monkeypatch.chdir themselves; their
    later override wins on the same monkeypatch instance.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_nauro_home(tmp_path, monkeypatch):
    """Point NAURO_HOME at tmp_path so tests never see the dev's real store.

    Mirrors the isolation rationale of ``_isolate_cwd``: a stray NAURO_HOME in
    the dev's shell would leak the real ``~/.nauro/`` into the suite. Tests that
    need a different layout override on the same monkeypatch instance; the
    later setenv wins.
    """
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
