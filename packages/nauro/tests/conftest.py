"""Shared pytest configuration and helpers for the nauro test suite."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from nauro_core.decision_model import Decision, format_decision

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp.tools import tool_get_context
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store

# Magic UUID4 used by every telemetry test that seeds a consented config.
# Centralized so a rotation in one file can't drift away from the rest.
TEST_ANONYMOUS_ID = "11111111-1111-4111-8111-111111111111"

# Fixed identity used by every cross-surface parity test to seed both the
# FilesystemStore and the CloudStore. Centralized so a change in one file
# can't drift away from the rest.
CROSS_SURFACE_USER_ID = "01TEST" + "0" * 20
CROSS_SURFACE_PROJECT_ID = "01TESTPROJECT00000000000"

# Shared moto bucket for cross-surface tests. Each fixture runs inside its
# own ``mock_aws()`` context, so the name only needs to be stable, not unique.
CROSS_SURFACE_BUCKET = "nauro-cross-surface-test"


def cloud_prefix(user_id: str, project_id: str) -> str:
    """Return the S3 key prefix a CloudStore reads/writes under for a project."""
    return f"users/{user_id}/projects/{project_id}"


def read_project_context(store_path: Path, level: int = 0) -> str:
    """Extract the ``content`` string from the ``tool_get_context`` envelope.

    Shared by tests that assert on the rendered context string without
    caring about the surrounding envelope fields.
    """
    return tool_get_context(store_path, level)["content"]


class V2Repo(NamedTuple):
    """Result of the canonical v2 registration body shared by parity fixtures."""

    pid: str
    store_path: Path
    repo: Path


def register_v2_repo(
    tmp_path: Path,
    name: str,
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
    mode: str = REPO_CONFIG_MODE_LOCAL,
    seed: str = "scaffold",
    save_config: bool = True,
    chdir: bool = True,
) -> V2Repo:
    """Run the canonical v2 registration body the local parity fixtures share.

    Creates ``tmp_path/"repo"``, registers a v2 project, optionally writes the
    per-repo config, seeds the store, and chdirs into the repo. ``seed`` selects
    the store body: ``"scaffold"`` runs ``scaffold_project_store``, ``"mkdir"``
    just creates the store directory, and ``"none"`` leaves it to the caller
    (which then writes its own content, e.g. via ``seed_decisions_into`` or a
    demo project). ``monkeypatch`` is required only when ``chdir`` is True.

    This body is v2-only by design: v1 ``register_project`` seeders are left
    untouched and must never be routed through here.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(name, [repo], mode=mode)
    if save_config:
        save_repo_config(repo, {"mode": mode, "id": pid, "name": name})
    if seed == "scaffold":
        scaffold_project_store(name, store_path)
    elif seed == "mkdir":
        store_path.mkdir(parents=True, exist_ok=True)
    if chdir:
        monkeypatch.chdir(repo)
    return V2Repo(pid, store_path, repo)


def seed_decisions_into(store_path: Path, *decisions: Decision) -> None:
    """Write decision files into ``store_path/"decisions"`` (creating the dir).

    Filenames follow the ``NNN-slugified-title.md`` rule the context and search
    parity fixtures both relied on.
    """
    decisions_dir = store_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for d in decisions:
        slug = d.title.lower().replace(" ", "-")
        (decisions_dir / f"{d.num:03d}-{slug}.md").write_text(format_decision(d))


@contextmanager
def moto_s3_bucket(monkeypatch, *, bucket: str = CROSS_SURFACE_BUCKET) -> Iterator[Any]:
    """Stand up a moto-mocked S3 bucket and point ``NAURO_S3_BUCKET`` at it.

    Yields the boto3 ``s3`` client so callers can seed objects before
    constructing a CloudStore (which reads ``NAURO_S3_BUCKET`` lazily). boto3
    and moto are imported lazily so this module stays importable in
    environments where neither package is installed; cross-surface tests gate
    on their availability via ``pytest.importorskip`` at their own module load.
    """
    import boto3
    import moto

    monkeypatch.setenv("NAURO_S3_BUCKET", bucket)
    with moto.mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=bucket)
        yield s3_client


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
def _neutralize_nauro_command_probe(monkeypatch):
    """Never spawn a real nauro binary, and reset the resolver cache per test.

    ``_find_nauro_command`` (setup) and ``nauro status`` liveness both go through
    ``nauro.cli.utils.probe_nauro_command`` — the single subprocess seam. Default
    it to "runs fine" and mark every path durable so surface-wiring tests take
    the historical fast path (record the interpreter-sibling, no warning) and get
    a valid absolute command without a subprocess. Tests that exercise
    dead/fragile wiring override these on their own monkeypatch instance (later
    setattr wins). The functools cache on the resolver is cleared so each test
    resolves fresh and any warnings emit deterministically.

    Probe/durability unit tests capture the real functions at import time (before
    this fixture patches) and call them directly, so they are unaffected.
    """
    from nauro.cli import utils as cli_utils
    from nauro.cli.commands import setup as setup_mod

    monkeypatch.setattr(cli_utils, "probe_nauro_command", lambda cmd, **kwargs: True)
    monkeypatch.setattr(cli_utils, "_is_durable_install_path", lambda path: True)
    setup_mod._find_nauro_command.cache_clear()
    yield
    setup_mod._find_nauro_command.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_nauro_home(tmp_path, monkeypatch):
    """Point NAURO_HOME at tmp_path so tests never see the dev's real store.

    Mirrors the isolation rationale of ``_isolate_cwd``: a stray NAURO_HOME in
    the dev's shell would leak the real ``~/.nauro/`` into the suite. Tests that
    need a different layout override on the same monkeypatch instance; the
    later setenv wins.
    """
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
