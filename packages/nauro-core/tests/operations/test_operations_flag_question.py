"""Kernel-level tests for ``operations.flag_question``.

Each test seeds an :class:`~nauro_core.operations.InMemoryStore` so the
scan-mint-insert plumbing exercises the locked Store protocol without
any filesystem dependency. Surface-level wiring (snapshot capture, push
hooks, length validation, similarity hinting, envelope-token rejection)
lives in the consumer package; the kernel must never reach into those
primitives.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nauro_core.constants import OPEN_QUESTIONS_MD
from nauro_core.operations import (
    FlagQuestionResult,
    InMemoryStore,
    flag_question,
)


def test_returns_result_type() -> None:
    result = flag_question(InMemoryStore(), "Should we ship X?")
    assert isinstance(result, FlagQuestionResult)


def test_empty_store_seeds_default_header_and_writes_q1() -> None:
    store = InMemoryStore()
    result = flag_question(store, "Should we ship X?")
    assert result.status == "ok"
    assert result.num == 1
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert content.startswith("# Open Questions")
    assert "- [Q1] Should we ship X?" in content


def test_existing_entries_mint_next_sequential_id() -> None:
    seed = "# Open Questions\n- [Q3] First seeded question\n- [Q5] Second seeded question\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    result = flag_question(store, "Third question")
    assert result.status == "ok"
    assert result.num == 6
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert "- [Q6] Third question" in content
    # The two seeded entries survive verbatim.
    assert "- [Q3] First seeded question" in content
    assert "- [Q5] Second seeded question" in content


def test_new_entry_inserts_after_header() -> None:
    seed = "# Open Questions\n- [Q1] First\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    flag_question(store, "Second")
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    lines = content.split("\n")
    # Header, then new entry on top, then prior entry.
    assert lines[0] == "# Open Questions"
    assert lines[1] == "- [Q2] Second"
    assert lines[2] == "- [Q1] First"


def test_new_entry_skips_leading_html_comment() -> None:
    seed = "# Open Questions\n<!-- managed automatically -->\n- [Q1] First\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    flag_question(store, "Second")
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    lines = content.split("\n")
    assert lines[0] == "# Open Questions"
    assert lines[1] == "<!-- managed automatically -->"
    assert lines[2] == "- [Q2] Second"
    assert lines[3] == "- [Q1] First"


def test_new_entry_skips_blank_line_after_header() -> None:
    seed = "# Open Questions\n\n- [Q1] First\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    flag_question(store, "Second")
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    lines = content.split("\n")
    assert lines[0] == "# Open Questions"
    assert lines[1] == ""
    assert lines[2] == "- [Q2] Second"
    assert lines[3] == "- [Q1] First"


def test_repeated_writes_accumulate_sequential_ids() -> None:
    store = InMemoryStore()
    first = flag_question(store, "One")
    second = flag_question(store, "Two")
    third = flag_question(store, "Three")
    assert (first.num, second.num, third.num) == (1, 2, 3)
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert content.count("- [Q") == 3


def test_context_argument_is_currently_ignored() -> None:
    """The adapter composes ``context`` into the question body, so the
    kernel's ``context`` parameter has no effect on the written line."""
    store = InMemoryStore()
    result = flag_question(store, "Composed body", context="should not appear")
    assert result.status == "ok"
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert "- [Q1] Composed body" in content
    assert "should not appear" not in content


def test_result_exclude_none_strips_unset_error() -> None:
    result = flag_question(InMemoryStore(), "One")
    dumped = result.model_dump(mode="json", exclude_none=True)
    assert dumped == {"status": "ok", "num": 1}


def test_result_is_frozen() -> None:
    result = flag_question(InMemoryStore(), "One")
    with pytest.raises(ValidationError):
        result.status = "ok"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FlagQuestionResult(status="ok", num=1, unexpected_field="value")


def test_store_field_absent_from_result_model_dump() -> None:
    result = flag_question(InMemoryStore(), "One")
    dumped = result.model_dump(mode="json")
    assert "store" not in dumped


# --- targets short-circuit ---


def _resolved_q5_seed() -> str:
    return (
        "# Open Questions\n"
        "\n"
        "- [Q1] still open\n"
        "- [Resolved by D42 on 2026-05-20] [Q5] already resolved by D42\n"
        "\n"
        "## Resolved\n"
    )


def test_targets_pointing_at_resolved_entry_short_circuits() -> None:
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _resolved_q5_seed()})
    before = store.read_file(OPEN_QUESTIONS_MD)
    result = flag_question(store, "duplicate of Q5", targets=["Q5"])
    assert result.status == "rejected"
    assert result.num is None
    assert result.error is not None
    assert result.error.kind == "rejected"
    assert "D42" in result.error.reason
    # No write happened on the rejection path.
    assert store.read_file(OPEN_QUESTIONS_MD) == before


def test_targets_pointing_at_open_entry_appends_normally() -> None:
    seed = "# Open Questions\n\n- [Q1] still open\n- [Q5] also still open\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    result = flag_question(store, "follow-up to Q5", targets=["Q5"])
    assert result.status == "ok"
    assert result.num == 6
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert "- [Q6] follow-up to Q5" in content


def test_targets_with_unknown_id_falls_through_and_appends() -> None:
    seed = "# Open Questions\n\n- [Q1] only known id\n"
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    result = flag_question(store, "new flag", targets=["Q99"])
    assert result.status == "ok"
    assert result.num == 2


def test_targets_none_skips_short_circuit_check() -> None:
    """``targets=None`` is the back-compat path; existing callers see no
    change. A resolved id in the file is ignored when targets isn't passed."""
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _resolved_q5_seed()})
    result = flag_question(store, "unrelated flag")
    assert result.status == "ok"
    # Q5 stays the highest existing num; mint Q6.
    assert result.num == 6


def test_targets_empty_list_skips_short_circuit_check() -> None:
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _resolved_q5_seed()})
    result = flag_question(store, "unrelated flag", targets=[])
    assert result.status == "ok"
    assert result.num == 6


def test_targets_with_multiple_ids_first_resolved_wins() -> None:
    """When more than one id matches, the kernel rejects on the first
    already-resolved hit and names that decision in the envelope."""
    seed = (
        "# Open Questions\n"
        "\n"
        "- [Resolved by D42 on 2026-05-20] [Q5] resolved by D42\n"
        "- [Resolved by D77 on 2026-05-21] [Q7] resolved by D77\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    result = flag_question(store, "dup", targets=["Q5", "Q7"])
    assert result.status == "rejected"
    assert result.error is not None
    # The envelope names the first matched id's resolving decision.
    assert "Q5" in result.error.reason
    assert "D42" in result.error.reason


def test_targets_with_legacy_timestamp_id_short_circuits() -> None:
    """Short-circuit treats legacy timestamp ids the same as Q### ids."""
    seed = (
        "# Open Questions\n"
        "\n"
        "- [Resolved by D99 on 2026-05-10] "
        "[2026-04-30 10:00 UTC] legacy resolved\n"
    )
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: seed})
    result = flag_question(store, "dup", targets=["2026-04-30 10:00 UTC"])
    assert result.status == "rejected"
    assert result.error is not None
    assert "D99" in result.error.reason


def test_short_circuit_envelope_mentions_working_copy_freshness() -> None:
    """The rejection envelope must call out the working-copy freshness
    bound so callers know stale local state may miss a remote resolution."""
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _resolved_q5_seed()})
    result = flag_question(store, "dup", targets=["Q5"])
    assert result.status == "rejected"
    assert result.error is not None
    assert "pull" in result.error.reason.lower()


# --- resolve action (resolved_by) ---


def _open_q1_q5_seed() -> str:
    return "# Open Questions\n\n- [Q1] still open\n- [Q5] also still open\n"


def _decisions(*nums: int) -> dict[str, str]:
    return {f"{n:03d}-some-decision": f"# Decision {n}\n" for n in nums}


def test_resolve_stamps_target_in_place_and_returns_ok() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()},
    )
    result = flag_question(store, targets=["Q5"], resolved_by="D42")
    assert result.status == "ok"
    assert result.num is None
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    # Q5 is stamped in place — its line keeps its position (after Q1) and the
    # entry block is never relocated under ## Resolved.
    lines = content.split("\n")
    q5_line = next(line for line in lines if "[Q5]" in line)
    assert q5_line.startswith("- [Resolved by D42 on ")
    assert "[Q1] still open" in content
    assert lines.index(q5_line) > lines.index("- [Q1] still open")


def test_resolve_does_not_append_a_question() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()},
    )
    flag_question(store, targets=["Q5"], resolved_by="D42")
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    # Only the two seeded ids exist; nothing was minted.
    assert content.count("- [Q") == 1  # Q1 (open); Q5 now carries the Resolved prefix.
    assert "[Q6]" not in content


def test_resolve_already_resolved_id_is_idempotent_ok() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _resolved_q5_seed()},
    )
    before = store.read_file(OPEN_QUESTIONS_MD)
    result = flag_question(store, targets=["Q5"], resolved_by="D42")
    # The append-path already-resolved short-circuit must NOT fire on the
    # resolve path; re-resolving the same id is a no-op success.
    assert result.status == "ok"
    after = store.read_file(OPEN_QUESTIONS_MD)
    assert after == before


def test_resolve_unparseable_decision_id_rejects() -> None:
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()})
    result = flag_question(store, targets=["Q5"], resolved_by="not-a-decision")
    assert result.status == "rejected"
    assert result.num is None
    assert result.error is not None
    assert "not-a-decision" in result.error.reason


def test_resolve_missing_decision_rejects_naming_number() -> None:
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()})
    result = flag_question(store, targets=["Q5"], resolved_by="D99")
    assert result.status == "rejected"
    assert result.error is not None
    assert "D99" in result.error.reason
    # No write occurred — the open question stays open.
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert "- [Q5] also still open" in content


def test_resolve_unknown_target_rejects_whole_call_naming_id() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()},
    )
    before = store.read_file(OPEN_QUESTIONS_MD)
    result = flag_question(store, targets=["Q5", "Q404"], resolved_by="D42")
    assert result.status == "rejected"
    assert result.error is not None
    assert "Q404" in result.error.reason
    # The whole call is rejected: Q5 must NOT be partially resolved.
    assert store.read_file(OPEN_QUESTIONS_MD) == before


def test_resolve_ambiguous_target_rejects_naming_id() -> None:
    seed = (
        "# Open Questions\n"
        "\n"
        "- [2026-04-30 10:00 UTC] first collision\n"
        "- [2026-04-30 10:00 UTC] second collision\n"
    )
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: seed},
    )
    result = flag_question(store, targets=["2026-04-30 10:00 UTC"], resolved_by="D42")
    assert result.status == "rejected"
    assert result.error is not None
    assert "2026-04-30 10:00 UTC" in result.error.reason


def test_resolve_with_no_targets_rejects() -> None:
    store = InMemoryStore(decisions=_decisions(42))
    result = flag_question(store, targets=[], resolved_by="D42")
    assert result.status == "rejected"
    assert result.error is not None


def test_resolve_missing_decision_reason_mentions_freshness() -> None:
    store = InMemoryStore(files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()})
    result = flag_question(store, targets=["Q5"], resolved_by="D99")
    assert result.status == "rejected"
    assert result.error is not None
    assert "pull" in result.error.reason.lower()


def test_resolve_uses_utc_now_date() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()},
    )
    today = datetime.now(timezone.utc).date().isoformat()
    flag_question(store, targets=["Q5"], resolved_by="D42")
    content = store.read_file(OPEN_QUESTIONS_MD)
    assert content is not None
    assert f"[Resolved by D42 on {today}]" in content


# --- neither / both ---


def test_neither_question_nor_resolved_by_rejects() -> None:
    store = InMemoryStore()
    result = flag_question(store)
    assert result.status == "rejected"
    assert result.error is not None
    assert "question" in result.error.reason.lower()


def test_both_question_and_resolved_by_rejects() -> None:
    store = InMemoryStore(
        decisions=_decisions(42),
        files={OPEN_QUESTIONS_MD: _open_q1_q5_seed()},
    )
    before = store.read_file(OPEN_QUESTIONS_MD)
    result = flag_question(store, question="a flag", targets=["Q5"], resolved_by="D42")
    assert result.status == "rejected"
    assert result.error is not None
    # Neither action ran — no append, no resolve.
    assert store.read_file(OPEN_QUESTIONS_MD) == before


# --- Import-graph negative constraint ---
#
# The kernel must not depend on snapshot capture, sync hooks, or
# pathlib — those are transport-side concerns that the locked Store
# protocol abstracts over. Pinning the constraint as an AST scan keeps
# the no-drift-by-construction guarantee from accidentally regressing
# through a casual import.


def _kernel_imports() -> set[str]:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nauro_core"
        / "operations"
        / "flag_question.py"
    )
    tree = ast.parse(module_path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add(f"{module}.{alias.name}")
                imported.add(alias.name)
                imported.add(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    return imported


def test_kernel_does_not_import_snapshot_or_sync_or_path() -> None:
    imported = _kernel_imports()
    forbidden_names = {
        "nauro.store.snapshot",
        "nauro.sync.hooks",
        "capture_snapshot",
        "push_after_write",
        "pull_before_session",
    }
    leaked = forbidden_names & imported
    assert not leaked, (
        f"kernel imports adapter-side primitives: {leaked}; "
        "those live on the transport, not in the kernel."
    )
    assert "pathlib" not in imported and "pathlib.Path" not in imported, (
        "kernel imports pathlib; the Store protocol abstracts paths as strings."
    )
