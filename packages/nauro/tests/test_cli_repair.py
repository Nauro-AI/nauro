"""Tests for the ``nauro repair`` command.

Command-and-adapter behaviour: the confirmation gate, what reaches disk, the
journal record, and the abort when the store moves under a confirmed plan.
Which shapes are repairable and which are refused is pinned in the nauro-core
suite and is not re-asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nauro_core.decision_model import Decision, format_decision
from typer.testing import CliRunner

import nauro.cli.commands.repair as repair_module
from nauro.cli.main import app
from nauro.store.journal import read_events
from nauro.store.registry import RegistryEntryV2, register_project_v2
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import write_decision_file

runner = CliRunner()

CHILD_STEM = "020-child"
TARGET_STEM = "019-target"


def _decision_md(num: int, *, supersedes: str | None = None) -> str:
    return format_decision(
        Decision(
            date="2026-03-15",
            confidence="high",
            num=num,
            title=f"Decision {num}",
            rationale=f"Rationale for decision {num}.",
            supersedes=supersedes,
        )
    )


def _new_store(tmp_path: Path, name: str = "repairproj") -> Path:
    _pid, store = register_project_v2(name, [tmp_path])
    scaffold_project_store(name, store)
    return store


def _seed_orphan(store: Path) -> None:
    write_decision_file(store, 20, "child", _decision_md(20, supersedes="19"))
    write_decision_file(store, 19, "target", _decision_md(19))


def _target_body(store: Path) -> str:
    return (store / "decisions" / f"{TARGET_STEM}.md").read_text(encoding="utf-8")


def test_nothing_to_repair_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    write_decision_file(store, 1, "settled", _decision_md(1))
    before = (store / "decisions" / "001-settled.md").read_text(encoding="utf-8")

    result = runner.invoke(app, ["repair", "--project", "repairproj"])

    assert result.exit_code == 0
    assert "Nothing to repair" in result.stdout
    assert (store / "decisions" / "001-settled.md").read_text(encoding="utf-8") == before


def test_prompt_names_both_decisions_and_the_exact_field_change(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="n\n")

    assert result.exit_code == 0
    assert "D20" in result.stdout
    assert "D19" in result.stdout
    assert "status active -> superseded" in result.stdout
    assert "superseded_by -> D20" in result.stdout
    assert "Apply this repair?" in result.stdout


def test_declining_writes_nothing(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    before = _target_body(store)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="n\n")

    assert result.exit_code == 0
    assert "No changes written." in result.stdout
    assert _target_body(store) == before


def test_confirming_flips_the_target_frontmatter(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    child_before = (store / "decisions" / f"{CHILD_STEM}.md").read_text(encoding="utf-8")

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert result.exit_code == 0
    after = _target_body(store)
    assert "status: superseded" in after
    assert "superseded_by: '20'" in after
    assert "Rationale for decision 19." in after
    # Only the target moves: the child that named it is untouched.
    assert (store / "decisions" / f"{CHILD_STEM}.md").read_text(encoding="utf-8") == child_before
    assert "Run 'nauro sync' to publish this change." in result.stdout


def test_repair_is_idempotent(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")
    after_first = _target_body(store)

    second = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert second.exit_code == 0
    assert "Nothing to repair" in second.stdout
    assert _target_body(store) == after_first


def test_journal_records_the_repair_with_cli_origin(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)

    runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    events = [e for e in read_events(store) if e.operation == repair_module.REPAIR_OPERATION]
    assert len(events) == 1
    event = events[0]
    assert event.status == "committed"
    assert event.target == '{"child":20,"target":19}'
    assert event.decision_id == TARGET_STEM
    assert event.origin is not None
    assert event.origin.transport == "cli"


def test_journal_failure_does_not_fail_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("journal unavailable")

    monkeypatch.setattr("nauro.store.journal.append_event", _boom)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert result.exit_code == 0
    assert "status: superseded" in _target_body(store)
    assert read_events(store) == []


def test_store_changing_under_a_confirmed_plan_aborts_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    real_planner = repair_module.plan_supersede_repair
    calls = {"count": 0}

    def _mutating_planner(fs_store):
        calls["count"] += 1
        if calls["count"] == 2:
            target = store / "decisions" / f"{TARGET_STEM}.md"
            target.write_text(
                target.read_text(encoding="utf-8") + "\nA later edit.\n", encoding="utf-8"
            )
        return real_planner(fs_store)

    monkeypatch.setattr(repair_module, "plan_supersede_repair", _mutating_planner)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert result.exit_code == 1
    after = _target_body(store)
    assert "status: active" in after
    assert "A later edit." in after


def test_refusal_prints_guidance_and_writes_nothing(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    write_decision_file(store, 21, "rival", _decision_md(21, supersedes="19"))
    before = _target_body(store)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert result.exit_code == 0
    assert "Left for manual review" in result.stdout
    assert "all claim to supersede D19" in result.stdout
    assert "Apply this repair?" not in result.stdout
    assert _target_body(store) == before


def test_two_independent_orphans_report_and_write_nothing(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    _seed_orphan(store)
    write_decision_file(store, 31, "other-child", _decision_md(31, supersedes="30"))
    write_decision_file(store, 30, "other-target", _decision_md(30))
    before = _target_body(store)

    result = runner.invoke(app, ["repair", "--project", "repairproj"], input="y\n")

    assert result.exit_code == 0
    assert "Supersede backref orphans (2)" in result.stdout
    assert "resolve these by hand" in result.stdout
    assert _target_body(store) == before


def test_unknown_project_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["repair", "--project", "nope"])
    assert result.exit_code == 1


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_both_registered_modes_are_eligible(mode: str) -> None:
    entry = RegistryEntryV2(
        name="repairproj",
        mode=mode,
        server_url="https://example.invalid" if mode == "cloud" else None,
    )
    assert repair_module.classify_repair_eligibility(entry).eligible is True


def test_store_without_a_registry_entry_is_refused() -> None:
    eligibility = repair_module.classify_repair_eligibility(None)
    assert eligibility.eligible is False
    assert "nauro status" in eligibility.guidance
