"""Shared pytest configuration and helpers for the nauro test suite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from nauro_core.decision_model import Decision, format_decision

from nauro.constants import DECISIONS_DIR, PROJECT_MD, REPO_CONFIG_MODE_LOCAL
from nauro.mcp.tools import tool_get_context
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store

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


def normalize_transcript(text: str, replacements: dict[str, str]) -> str:
    """Replace each volatile substring in ``text`` with its placeholder.

    Pure helper for transcript pins: callers pass a mapping of volatile
    values (temp dirs, resolved command paths) to stable placeholders such
    as ``{TMP}`` or ``{NAURO_CMD}``. Replacements are applied in mapping
    order, so callers with nested volatile values list the longer needle
    first.
    """
    for needle, placeholder in replacements.items():
        text = text.replace(needle, placeholder)
    return text


def snapshot_tree(root: Path) -> list[str]:
    """Sorted relative POSIX paths of all files under ``root``.

    ``.git`` subtrees are excluded so inventory assertions stay independent
    of the git version's on-disk layout.
    """
    paths = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        paths.append(rel.as_posix())
    return sorted(paths)


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
    creates the minimal valid empty-store structure, and ``"none"`` leaves it
    to the caller (which then writes its own content, e.g. via
    ``seed_decisions_into`` or a demo project). ``monkeypatch`` is required
    only when ``chdir`` is True.

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
        (store_path / PROJECT_MD).touch()
        (store_path / DECISIONS_DIR).mkdir()
    if chdir:
        monkeypatch.chdir(repo)
    return V2Repo(pid, store_path, repo)


def seed_decisions_into(store_path: Path, *decisions: Decision) -> None:
    """Write decision files into ``store_path/"decisions"`` (creating the dir).

    Filenames follow the ``NNN-slugified-title.md`` rule the context and search
    parity fixtures both relied on.
    """
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / PROJECT_MD).touch(exist_ok=True)
    decisions_dir = store_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    for d in decisions:
        slug = d.title.lower().replace(" ", "-")
        (decisions_dir / f"{d.num:03d}-{slug}.md").write_text(format_decision(d))


def write_decision_file(store: Path, num: int, slug: str, content: str) -> None:
    """Write raw ``content`` to ``store/decisions/NNN-slug.md`` verbatim.

    Unlike ``seed_decisions_into``, this takes an already-rendered body so tests
    can seed malformed or hand-crafted decision files.
    """
    (store / DECISIONS_DIR / f"{num:03d}-{slug}.md").write_text(content, encoding="utf-8")


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


def seed_auth_config(
    *,
    variant: str = "local",
    access_token: str | None = None,
    refresh_token: str = "refresh_orig",
    sub: str = "auth0|test",
) -> None:
    """Write an ``auth`` block via ``save_config`` in one of two seeder shapes.

    ``variant="local"`` mirrors ``nauro auth login`` output (keys
    ``access_token, sub``); ``variant="sync"`` adds a refresh token and orders
    keys ``sub, access_token, refresh_token`` the way the sync suites seeded it.
    Writing through ``save_config`` keeps the serialized config byte-identical to
    the hand-written seeders. ``access_token`` defaults to ``"test-token"`` for
    the local shape and ``"tok_orig"`` for the sync shape.
    """
    if access_token is None:
        access_token = "test-token" if variant == "local" else "tok_orig"
    if variant == "local":
        auth = {"access_token": access_token, "sub": sub}
    else:
        auth = {"sub": sub, "access_token": access_token, "refresh_token": refresh_token}
    save_config({"auth": auth})


def make_nauro_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirname: str = ".nauro",
    chdir_repo: bool = False,
) -> Path:
    """Create a temp NAURO_HOME under ``tmp_path`` and point the env var at it.

    ``chdir_repo`` additionally creates a sibling ``repo`` directory and
    changes into it.
    """
    home = tmp_path / dirname
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    if chdir_repo:
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
    return home


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    """Canonical temp NAURO_HOME at ``tmp_path/".nauro"``."""
    return make_nauro_home(tmp_path, monkeypatch)


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
    ``nauro.cli.nauro_command.probe_nauro_command`` — the single subprocess seam. Default
    it to "runs fine" and mark every path durable so surface-wiring tests take
    the historical fast path (record the interpreter-sibling, no warning) and get
    a valid absolute command without a subprocess. Tests that exercise
    dead/fragile wiring override these on their own monkeypatch instance (later
    setattr wins). The functools cache on the resolver is cleared so each test
    resolves fresh and any warnings emit deterministically.

    Probe/durability unit tests capture the real functions at import time (before
    this fixture patches) and call them directly, so they are unaffected.
    """
    from nauro.cli import nauro_command

    monkeypatch.setattr(nauro_command, "probe_nauro_command", lambda cmd, **kwargs: True)
    monkeypatch.setattr(nauro_command, "_is_durable_install_path", lambda path: True)
    nauro_command._find_nauro_command.cache_clear()
    nauro_command._find_nauro_codex_hook_command.cache_clear()
    yield
    nauro_command._find_nauro_codex_hook_command.cache_clear()
    nauro_command._find_nauro_command.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_nauro_home(tmp_path, monkeypatch):
    """Point NAURO_HOME at tmp_path so tests never see the dev's real store.

    Mirrors the isolation rationale of ``_isolate_cwd``: a stray NAURO_HOME in
    the dev's shell would leak the real ``~/.nauro/`` into the suite. Tests that
    need a different layout override on the same monkeypatch instance; the
    later setenv wins.
    """
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point HOME at a per-test sandbox so user-global writers never touch it.

    NAURO_HOME only covers the store; the user-global artifact writers
    (``~/.codex/config.toml``, ``~/.claude.json``, ``~/.claude/skills``,
    ``~/.agents/skills``) resolve through ``Path.home()``. A test that
    exercises them without its own HOME patch rewrites the developer's real
    config — a full suite run did exactly that to ``~/.codex/config.toml``
    before this guard existed. CI never catches the escape because runner
    homes are throwaway. The sandbox is a subdirectory so tests that pin
    filesystem inventories over ``tmp_path`` and set ``HOME`` themselves
    (later setenv wins) keep their own layout. The sandbox is not
    pre-created: several tests assert exact ``tmp_path`` contents, and the
    user-global writers create their own parents.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
