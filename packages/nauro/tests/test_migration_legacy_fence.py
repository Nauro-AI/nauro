from __future__ import annotations

import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Event

import pytest
from filelock import Timeout
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmissionError,
    inspect_migration,
    migration_lock_path,
    migration_write_guard,
)
from nauro.sync import hooks, pull, push
from nauro.sync import migration_admission as migration
from nauro.sync.transfer import NullReporter
from tests.test_cli_repair import _seed_orphan
from tests.test_generation_migration_plan import _assessment
from tests.test_migration_source_check import reassess_after_writer


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path / "home"))

    def forbidden(*args, **kwargs):
        pytest.fail("External network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    binding, assessment = _assessment(tmp_path)
    record = migration.save_migration_assessment(prepare_legacy_migration_plan(assessment))
    return binding, record, assessment.projection


@pytest.mark.parametrize("command", ["note", "import", "questions", "repair", "sync"])
def test_legacy_command_holds_fence_through_ancillary_work(saved, tmp_path, monkeypatch, command):
    binding, record, projection = saved
    module = import_module(f"nauro.cli.commands.{'import_cmd' if command == 'import' else command}")
    monkeypatch.setattr(
        module, "resolve_target_project", lambda *_: ("Synthetic", binding.store_path)
    )
    arguments = [command]
    if command == "note":
        arguments += ["Synthetic note"]
    elif command == "import":
        source = tmp_path / "adrs"
        source.mkdir()
        (source / "0001-example.md").write_text("# Example\n\nSynthetic rationale\n")
        arguments += ["--adr", str(source)]
    elif command == "questions":
        (binding.store_path / "open-questions.md").write_text(
            "# Open questions\n\n- [2026-03-15 12:00 UTC] Synthetic question?\n"
        )
        arguments += ["migrate"]
    elif command == "repair":
        for decision in (binding.store_path / "decisions").glob("*.md"):
            decision.unlink()
        _seed_orphan(binding.store_path)
        monkeypatch.setattr(module, "_registry_entry", lambda *_: {})
    else:
        monkeypatch.setattr(module, "refresh_command", lambda *a, **k: False)
        monkeypatch.setattr(module, "_pull_from_cloud", lambda *a, **k: pull.PullReport())
        monkeypatch.setattr(module, "push_store_to_cloud", lambda *a, **k: push.PushReport())
        monkeypatch.setattr(module, "warn_then_regen", lambda *a, **k: [])
    seam = "capture_snapshot" if command == "sync" else "run_post_commit"
    original = getattr(module, seam)
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, seam, delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(CliRunner().invoke, app, arguments, input="y\n")
        try:
            assert entered.wait(10)
            with pytest.raises(Timeout):
                migration.decide_migration_assessment(record, preserve=True)
            assert inspect_migration(binding.store_path) == record
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.exit_code == 0, result.output
    assert reassess_after_writer(record, projection).phase == "blocked"
    before = {p: p.read_bytes() for p in binding.store_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, arguments, input="y\n")
    assert result.exit_code == 1, result.output
    assert before == {p: p.read_bytes() for p in binding.store_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("direction", ["pull", "push"])
def test_transfer_fence_precedes_root_preparation_and_network(saved, monkeypatch, direction):
    binding, record, projection = saved
    migration.decide_migration_assessment(record, preserve=True)
    binding.store_path.rename(binding.store_path.with_name("retained"))
    module = pull if direction == "pull" else push

    def forbidden(*args, **kwargs):
        pytest.fail("Blocked transfer reached root preparation")

    monkeypatch.setattr(module, "_prepare_store_root", forbidden)
    with pytest.raises(MigrationAdmissionError, match="conversion is incomplete"):
        if direction == "pull":
            pull.run_pull(binding.project_id, binding.store_path, NullReporter())
        else:
            push.push_changed_files(binding.project_id, binding.store_path)
    assert not binding.store_path.exists()


@pytest.mark.parametrize("direction", ["pull", "push"])
def test_delayed_transfer_prevents_conversion(saved, monkeypatch, direction):
    binding, record, projection = saved
    entered, release = Event(), Event()
    module = pull if direction == "pull" else push

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return pull.PullReport() if direction == "pull" else push.PushReport()

    monkeypatch.setattr(
        module, f"_{'run_pull' if direction == 'pull' else 'push_changed_files'}_locked", delayed
    )
    with ThreadPoolExecutor(max_workers=1) as pool:

        def function():
            if direction == "push":
                return push.push_changed_files(binding.project_id, binding.store_path)
            return pull.run_pull(binding.project_id, binding.store_path, NullReporter())

        future = pool.submit(function)
        try:
            assert entered.wait(10)
            with pytest.raises(Timeout):
                migration.decide_migration_assessment(record, preserve=True)
        finally:
            release.set()
        future.result(timeout=10)
    assert reassess_after_writer(record, projection).phase == "blocked"


def test_nested_fence_keeps_other_processes_out_until_outer_release(saved):
    binding, _, _ = saved
    script = """
import sys
from filelock import FileLock, Timeout
try:
    with FileLock(sys.argv[1], timeout=0):
        pass
except Timeout:
    sys.exit(7)
"""

    def competing_process():
        return subprocess.run(
            [sys.executable, "-c", script, str(migration_lock_path(binding.store_path))],
            capture_output=True,
            timeout=10,
            check=False,
        ).returncode

    with migration_write_guard(binding.store_path):
        with migration_write_guard(binding.store_path):
            assert competing_process() == 7
        assert competing_process() == 7
    assert competing_process() == 0


def test_nested_push_hook_completes_without_deadlock(saved, monkeypatch):
    binding, _, _ = saved
    monkeypatch.setattr(hooks, "load_access_token", lambda: "synthetic")
    monkeypatch.setattr(hooks, "is_cloud_project", lambda *_: True)
    monkeypatch.setattr(
        push, "_push_changed_files_locked", lambda *a: push.PushReport(verified=("state.md",))
    )
    with migration_write_guard(binding.store_path):
        assert hooks.push_after_write(binding.project_id, binding.store_path).verified == (
            "state.md",
        )


def test_nonblocking_nested_acquisition_still_excludes_other_threads(saved):
    binding, _, _ = saved
    with migration_write_guard(binding.store_path), ThreadPoolExecutor(max_workers=1) as pool:

        def contender():
            with migration_write_guard(binding.store_path, timeout=0):
                pytest.fail("Another thread entered the held fence")

        future = pool.submit(contender)
        with pytest.raises(MigrationAdmissionError, match="busy"):
            future.result(timeout=10)
