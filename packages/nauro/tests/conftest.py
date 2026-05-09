"""Shared pytest configuration.

Applies nauro config (e.g. api_key → ANTHROPIC_API_KEY) to the environment
so integration tests can use credentials configured via `nauro config set`
without requiring explicit env var injection.
"""

import pytest

from nauro.store.config import apply_config_to_env

apply_config_to_env()


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Chdir every test into tmp_path so CWD walk-up resolution doesn't leak.

    Several store/resolution paths walk up from `Path.cwd()` looking for
    ``.nauro/config.json``. If pytest is run from inside an adopted repo
    (e.g. the nauro repo dogfood-adopting itself), that walk finds a real
    config and trips ID-mismatch errors in tests that pass project= directly.
    Tests that need a specific CWD use monkeypatch.chdir themselves; their
    later override wins on the same monkeypatch instance.
    """
    monkeypatch.chdir(tmp_path)
