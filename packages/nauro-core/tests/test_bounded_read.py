"""Bounded rendered reads: truncation matrix, open-questions split, context gate.

Pins the renderer-channel guards:

* ``render_get_raw_file`` bounds file content by canonical path: head-keep
  by default, tail-keep for append-at-end files, a larger per-brief budget
  for ``context/*.md``, and a structure-aware split for
  ``open-questions.md`` (exact ``## Resolved`` header rule, reserved
  preamble tail window, whole-entry discovery-pointer extraction).
* ``render_get_context`` withholds an over-budget body behind a compact
  guard report; the gate is size-keyed, not level-keyed.
* Both renderers are total: malformed envelopes, absent kwargs, and
  non-string bodies never raise (a raising renderer would trigger the
  full-envelope JSON fallback, which is exactly what the guards exist to
  prevent).
"""

from __future__ import annotations

from nauro_core.constants import (
    BOUNDED_READ_RECOVERY,
    CHARS_PER_TOKEN,
    CONTEXT_FILE_CHAR_BUDGET,
    CONTEXT_GUARD_REPORT,
    L2_CHAR_BUDGET,
    OPEN_QUESTIONS_MID_ELISION_MARKER,
    OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER,
    POINTER_BLOCK_HEADER,
    POINTER_EXTRACT_LIMIT,
    POINTER_OVERFLOW_TRAILER,
    QUESTION_ENTRY_CHAR_BUDGET,
    RAW_FILE_CHAR_BUDGET,
    RAW_FILE_HEAD_CUT_MARKER,
    RAW_FILE_TAIL_CUT_MARKER,
)
from nauro_core.questions import QUESTION_TRUNCATION_POINTER, OpenQuestionsFile
from nauro_core.renderers import RENDERERS, render_get_context, render_get_raw_file

OQ = "open-questions.md"


def _raw(content: str, path: str | None = None) -> str:
    return render_get_raw_file({"store": "local", "content": content}, path=path)


def _recovery(path: str) -> str:
    return BOUNDED_READ_RECOVERY.format(path=path)


def _head_marker(omitted: int, path: str) -> str:
    return RAW_FILE_HEAD_CUT_MARKER.format(
        omitted=f"{omitted:,}", path=path, recovery=_recovery(path)
    )


def _tail_marker(omitted: int, path: str) -> str:
    return RAW_FILE_TAIL_CUT_MARKER.format(
        omitted=f"{omitted:,}", path=path, recovery=_recovery(path)
    )


def _lines(count: int, tag: str = "x", width: int = 100) -> str:
    """``count`` newline-terminated lines of exactly ``width`` chars each."""
    out = []
    for i in range(count):
        body = f"{tag}{i:05d} " + "q" * width
        out.append(body[: width - 1] + "\n")
    return "".join(out)


class TestHeadKeep:
    def test_exact_budget_is_byte_verbatim(self):
        content = _lines(400)
        assert len(content) == RAW_FILE_CHAR_BUDGET
        assert _raw(content, "stack.md") == content

    def test_budget_plus_one_truncates_at_last_newline(self):
        content = _lines(400) + "z"
        expected = content[:39_999] + "\n" + _head_marker(2, "stack.md")
        assert _raw(content, "stack.md") == expected

    def test_no_newline_hard_cut_at_exact_budget(self):
        content = "a" * (RAW_FILE_CHAR_BUDGET + 123)
        expected = content[:RAW_FILE_CHAR_BUDGET] + "\n" + _head_marker(123, "stack.md")
        assert _raw(content, "stack.md") == expected

    def test_unknown_path_bounded_under_default_budget(self):
        content = "a" * (RAW_FILE_CHAR_BUDGET + 1)
        out = _raw(content, None)
        assert out == content[:RAW_FILE_CHAR_BUDGET] + "\n" + _head_marker(1, "<path>")


class TestTailKeep:
    def test_exact_budget_is_byte_verbatim(self):
        content = _lines(400)
        assert _raw(content, "state_history.md") == content

    def test_budget_plus_one_keeps_tail_from_first_newline(self):
        content = "z" + _lines(400)
        expected = _tail_marker(101, "state_history.md") + "\n" + content[101:]
        assert _raw(content, "state_history.md") == expected

    def test_no_newline_hard_cut_keeps_exactly_budget(self):
        content = "a" * (RAW_FILE_CHAR_BUDGET + 50)
        expected = _tail_marker(50, "state_history.md") + "\n" + content[50:]
        assert _raw(content, "state_history.md") == expected


class TestBriefBudget:
    def test_brief_at_cap_renders_whole(self):
        content = "b" * CONTEXT_FILE_CHAR_BUDGET
        assert _raw(content, "context/shared-brief.md") == content

    def test_brief_over_cap_is_bounded(self):
        path = "context/shared-brief.md"
        content = "b" * (CONTEXT_FILE_CHAR_BUDGET + 1)
        expected = content[:CONTEXT_FILE_CHAR_BUDGET] + "\n" + _head_marker(1, path)
        assert _raw(content, path) == expected

    def test_oversized_locally_retained_brief_is_bounded(self):
        path = "context/big.md"
        content = _lines(700)  # 70,000 chars: over the sync push cap, kept locally
        expected = content[:51_199] + "\n" + _head_marker(18_801, path)
        assert _raw(content, path) == expected


class TestContextGate:
    def test_at_budget_is_byte_identical(self):
        body = "c" * L2_CHAR_BUDGET
        assert render_get_context({"store": "local", "content": body}) == body

    def test_over_budget_returns_guard_report(self):
        size = L2_CHAR_BUDGET + 1
        body = "c" * size
        expected = CONTEXT_GUARD_REPORT.format(
            level_clause="",
            chars=f"{size:,}",
            tokens=f"{size // CHARS_PER_TOKEN:,}",
            budget=f"{L2_CHAR_BUDGET:,}",
        )
        assert render_get_context({"store": "local", "content": body}) == expected

    def test_gate_is_size_keyed_not_level_keyed(self):
        body = "c" * (L2_CHAR_BUDGET + 5)
        out = render_get_context({"content": body}, level="L0")
        assert "(level L0)" in out
        assert "c" * 100 not in out

    def test_int_level_tailors_wording(self):
        body = "c" * (L2_CHAR_BUDGET + 5)
        assert "(level L2)" in render_get_context({"content": body}, level=2)

    def test_remote_context_key_gates_and_passes(self):
        big = "c" * (L2_CHAR_BUDGET + 1)
        assert "Context withheld" in render_get_context({"context": big})
        assert render_get_context({"context": "small body"}) == "small body"

    def test_under_budget_with_level_kwarg_unchanged(self):
        assert render_get_context({"content": "hello"}, level="L2") == "hello"


class TestTotality:
    def test_empty_envelope_get_raw_file(self):
        assert render_get_raw_file({}) == ""

    def test_non_string_content_get_raw_file(self):
        assert render_get_raw_file({"content": 42}, path="stack.md") == "42"
        render_get_raw_file({"content": ["a", "b"]}, path=None)

    def test_non_string_path_treated_as_absent(self):
        out = render_get_raw_file({"content": "x" * 50_000}, path=123)
        assert "<path>" in out

    def test_non_string_context_body(self):
        render_get_context({"content": {"k": 1}})
        render_get_context({"context": 7})

    def test_weird_level_over_budget_never_raises(self):
        out = render_get_context({"content": "x" * (L2_CHAR_BUDGET + 1)}, level=object())
        assert "Context withheld" in out
        assert "(level" not in out

    def test_malformed_open_questions_never_raises(self):
        _raw("\x00" * 50_000, OQ)
        _raw("- [broken\n" * 8_000, OQ)
        _raw(("### topic\nbody\n" + "- [Q0] bad num\n") * 3_000, OQ)

    def test_registry_renderers_accept_no_kwargs(self):
        assert RENDERERS["get_raw_file"]({"content": "hi"}) == "hi"
        assert RENDERERS["get_context"]({"content": "hi"}) == "hi"

    def test_malformed_guidance_never_bypasses_raw_file_bound(self):
        # A non-string guidance field must not raise: a raising renderer
        # falls back to the full JSON envelope, which with an oversized
        # content field would bypass the bound entirely.
        out = render_get_raw_file({"guidance": 42, "content": "a" * 50_000}, path="stack.md")
        assert "chars omitted from the end of stack.md" in out
        assert len(out) < 41_000

    def test_malformed_guidance_ignored_when_body_empty(self):
        assert render_get_raw_file({"guidance": 42}) == ""
        assert render_get_context({"guidance": ["not", "a", "string"]}) == ""

    def test_malformed_available_files_never_triggers_json_fallback(self):
        envelope = {
            "error": {"kind": "error", "reason": "File not found: nope.md"},
            "available_files": 7,
            "content": "a" * 100_000,
        }
        out = render_get_raw_file(envelope)
        assert out == "Error: File not found: nope.md"

    def test_non_string_available_files_entries_dropped(self):
        envelope = {
            "error": {"kind": "error", "reason": "File not found: nope.md"},
            "available_files": ["project.md", 5, None],
        }
        out = render_get_raw_file(envelope)
        assert "  - project.md" in out
        assert "5" not in out

    def test_malformed_guidance_never_bypasses_context_gate(self):
        out = render_get_context({"guidance": 42, "content": "c" * (L2_CHAR_BUDGET + 1)})
        assert "Context withheld" in out
        assert "c" * 100 not in out


class TestOpenQuestionsSplit:
    def test_no_divider_two_region_split_exact(self):
        # 50,000 chars of open content, no ## Resolved header: whole file is
        # open, split into the 30K head window and the reserved 10K tail
        # window ending at the file's last character.
        content = _lines(500)
        mid = OPEN_QUESTIONS_MID_ELISION_MARKER.format(
            omitted=f"{10_101:,}", recovery=_recovery(OQ)
        )
        expected = content[:29_999] + "\n" + mid + "\n" + content[40_100:]
        out = _raw(content, OQ)
        assert out == expected
        assert "Resolved section elided" not in out

    def test_unresolved_threads_header_is_not_a_divider(self):
        content = (
            "# Open Questions\n" + _lines(250, "a") + "## Unresolved threads\n" + _lines(250, "b")
        )
        # Premise: the parser's substring heuristic WOULD treat this header
        # as the resolved divider; the bounded read must not.
        assert OpenQuestionsFile.parse(content).resolved_divider_idx is not None
        out = _raw(content, OQ)
        assert "Resolved section elided" not in out
        assert "## Unresolved threads" in out
        assert "b00249" in out  # newest entries survive in the reserved tail

    def test_indented_resolved_header_still_divides(self):
        # The exact-header rule is a whitespace-stripped comparison over
        # source lines: "  ## Resolved  " divides even though the parser
        # never mints a HeaderBlock for an indented header line.
        content = (
            "# Open Questions\n"
            + _lines(200, "p")
            + "  ## Resolved  \n"
            + "- [Q60] BRIEF: context/q.md - retired pointer\n"
            + _lines(300, "r")
        )
        resolved_start = content.index("  ## Resolved")
        out = _raw(content, OQ)
        assert (
            OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER.format(
                omitted=f"{len(content) - resolved_start:,}", recovery=_recovery(OQ)
            )
            in out
        )
        assert "[Q60]" not in out  # under the divider: retired, never extracted
        assert POINTER_BLOCK_HEADER not in out
        assert "p00199" in out  # open preamble renders whole
        assert "r00000" not in out

    def test_post_resolved_active_section_retained_exact(self):
        preamble = "# Open Questions\n" + _lines(50, "p")  # 5,017
        resolved = "## Resolved\n" + _lines(400, "r")  # 40,012
        post = "## Active\n" + _lines(20, "c")  # 2,010
        content = preamble + resolved + post
        marker = OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER.format(
            omitted=f"{40_012:,}", recovery=_recovery(OQ)
        )
        expected = content[:5_017] + "\n" + marker + "\n" + content[5_017 + 40_012 :]
        out = _raw(content, OQ)
        assert out == expected
        assert "## Active" in out
        assert "c00019" in out

    def test_realistic_merge_fixture_extracts_stranded_pointer(self):
        # Local preamble > 30K, then a remote-only run opening with a fresh
        # discovery pointer followed by > 10K of older remote-only entries,
        # then > 10K resolved history: the pointer lands outside both windows
        # and must survive via the extraction block.
        pointer_line = "- [Q900] BRIEF: context/shared-brief.md - " + "d" * 600 + "\n"
        content = (
            "# Open Questions\n"
            + _lines(310, "lo")
            + pointer_line
            + _lines(120, "rm")
            + "## Resolved\n"
            + _lines(120, "rs")
        )
        out = _raw(content, OQ)

        assert out.count("- [Q900]") == 1
        assert QUESTION_TRUNCATION_POINTER in out  # entry > 480 chars, truncated
        assert "d" * 600 not in out
        assert "rs00000" not in out  # resolved history elided
        assert (
            OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER.format(
                omitted=f"{12_012:,}", recovery=_recovery(OQ)
            )
            in out
        )

        mid_idx = out.index(" chars of older open entries omitted here")
        block_idx = out.index(POINTER_BLOCK_HEADER)
        pointer_idx = out.index("- [Q900]")
        tail_idx = out.index("rm00119")
        assert mid_idx < block_idx < pointer_idx < tail_idx

    def test_head_boundary_straddling_pointer_excluded_whole(self):
        entry = (
            "- [Q800] BRIEF: context/x.md - "
            + "e" * 118
            + "\n"  # 150 chars
            + ("  cont " + "f" * 142 + "\n") * 3  # 3 x 150 chars
        )
        content = _lines(298, "ha") + entry + _lines(150, "hb")
        out = _raw(content, OQ)
        # Newline-aligned head cut (29,949) falls inside the entry; the cut
        # shifts back to exclude the whole entry, which is extracted once.
        assert out.count("- [Q800]") == 1
        assert out.index(POINTER_BLOCK_HEADER) < out.index("- [Q800]")
        assert "  cont" in out
        assert QUESTION_TRUNCATION_POINTER in out  # 599-char entry truncated at 480
        assert "ha00297" in out  # head window ends at the filler boundary
        assert (
            OPEN_QUESTIONS_MID_ELISION_MARKER.format(omitted=f"{5_701:,}", recovery=_recovery(OQ))
            in out
        )

    def test_tail_boundary_straddling_pointer_excluded_whole(self):
        entry = (
            "- [Q801] RESUME: context/y.md - "
            + "e" * 117
            + "\n"  # 150 chars
            + ("  cont " + "f" * 142 + "\n") * 3  # 3 x 150 chars
        )
        content = _lines(350, "ta") + entry + _lines(97, "tb")
        out = _raw(content, OQ)
        # The tail window's aligned start (35,450) falls inside the entry;
        # the start shifts past the entry, which is extracted once.
        assert out.count("- [Q801]") == 1
        assert out.index(POINTER_BLOCK_HEADER) < out.index("- [Q801]")
        assert out.index("- [Q801]") < out.index("tb00000")
        assert (
            OPEN_QUESTIONS_MID_ELISION_MARKER.format(omitted=f"{5_601:,}", recovery=_recovery(OQ))
            in out
        )

    def test_pointer_inside_retained_window_not_extracted(self):
        pointer_line = "- [Q810] BRIEF: context/z.md - fully retained pointer\n"
        content = pointer_line + _lines(450)
        out = _raw(content, OQ)
        assert out.count("- [Q810]") == 1
        assert POINTER_BLOCK_HEADER not in out
        assert out.index("- [Q810]") < out.index(" chars of older open entries omitted here")

    def test_resolved_and_annotated_pointers_not_extracted(self):
        annotated = "- [Resolved by D10 on 2026-01-01] [Q51] BRIEF: context/y.md - old\n"
        content = (
            "# Open Questions\n"
            + _lines(305, "ra")
            + annotated
            + _lines(110, "rb")
            + "## Resolved\n"
            + "- [Q50] BRIEF: context/z.md - retired\n"
            + _lines(30, "rc")
        )
        out = _raw(content, OQ)
        assert "[Q51]" not in out  # resolved_by-annotated: elided, never extracted
        assert "[Q50]" not in out  # physically under ## Resolved: retired history
        assert POINTER_BLOCK_HEADER not in out

    def test_extraction_capped_at_twenty_in_file_order_with_trailer(self):
        pointers = "".join(
            f"- [Q{100 + i}] BRIEF: context/p{i}.md - stranded item number {i}\n"
            for i in range(23)
        )
        content = _lines(302, "oa") + pointers + _lines(110, "ob")
        out = _raw(content, OQ)

        for i in range(POINTER_EXTRACT_LIMIT):
            assert out.count(f"- [Q{100 + i}]") == 1
        for i in range(POINTER_EXTRACT_LIMIT, 23):
            assert f"[Q{100 + i}]" not in out
        assert POINTER_OVERFLOW_TRAILER.format(overflow=3, recovery=_recovery(OQ)) in out
        assert out.index("- [Q100]") < out.index("- [Q101]") < out.index("- [Q119]")

        block_start = out.index(POINTER_BLOCK_HEADER)
        block_end = out.index("ob00", block_start)
        per_entry_ceiling = QUESTION_ENTRY_CHAR_BUDGET + len(QUESTION_TRUNCATION_POINTER) + 2
        assert block_end - block_start <= (
            len(POINTER_BLOCK_HEADER) + POINTER_EXTRACT_LIMIT * per_entry_ceiling
        )

    def test_combined_region_post_cut_pointers_in_post_span(self):
        post = (
            "## Later notes\n"
            + "- [Q700] BRIEF: context/a.md - fresh follow-up\n"
            + _lines(120, "pb")
            + "- [Q701] SELECT: context/b.md - candidate set\n"
        )
        content = _lines(450, "pa") + "## Resolved\n" + _lines(100, "pr") + post
        out = _raw(content, OQ)

        # The preamble tail reservation holds even with post-resolved content.
        assert "pa00449" in out  # last preamble line (tail window)
        assert "pa00351" in out  # first line of the reserved tail
        assert "pa00350" not in out.split(POINTER_BLOCK_HEADER)[0]  # elided middle

        assert (
            OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER.format(
                omitted=f"{10_012:,}", recovery=_recovery(OQ)
            )
            in out
        )
        assert "pr00000" not in out
        assert "## Later notes" in out

        post_marker_idx = out.index(" chars omitted from the sections after ## Resolved")
        assert out.count(POINTER_BLOCK_HEADER) == 1
        assert post_marker_idx < out.index(POINTER_BLOCK_HEADER)
        # Q700 fits inside the remaining post budget and stays retained ahead
        # of the cut; Q701 sits in the elided post span and surfaces in that
        # span's extraction block after the marker. Neither is duplicated.
        assert out.index("- [Q700]") < post_marker_idx < out.index("- [Q701]")
        assert out.count("- [Q700]") == 1
        assert out.count("- [Q701]") == 1
