"""Shared pytest configuration and helpers for the nauro test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Magic UUID4 used by every telemetry test that seeds a consented config.
# Centralized so a rotation in one file can't drift away from the rest.
TEST_ANONYMOUS_ID = "11111111-1111-4111-8111-111111111111"


class FakeClient:
    """Capture-only stand-in for ``nauro.telemetry.client._client``.

    Records every ``capture(event, distinct_id, properties)`` call into
    ``events`` so tests can assert on the emitted payload shape. Tests that
    need to assert call ordering (alias-then-set semantics) use the
    ordered-tuple variant in test_identity_lifecycle.py instead.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def capture(
        self,
        event: str,
        distinct_id: str,
        properties: dict[str, Any],
    ) -> None:
        self.events.append({"event": event, "distinct_id": distinct_id, "properties": properties})


def seed_consented_config(home: Path, *, enabled: bool) -> str:
    """Write a fully consented telemetry config under ``home/config.json``.

    Returns the seeded anonymous_id so tests that need to compare against the
    persisted value have a single source of truth.
    """
    (home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": TEST_ANONYMOUS_ID,
                    "enabled": enabled,
                    "consent_version": 1,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )
    return TEST_ANONYMOUS_ID


@pytest.fixture
def telemetry_key(monkeypatch):
    """Set the PostHog key env var so ``_should_emit`` returns True for enabled tests."""
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")


@pytest.fixture
def fake_posthog(monkeypatch):
    """Swap ``nauro.telemetry.client._client`` for a ``FakeClient`` and reset after."""
    import nauro.telemetry.client as client_mod

    fake = FakeClient()
    client_mod._client = fake
    yield fake
    client_mod._client = None


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
