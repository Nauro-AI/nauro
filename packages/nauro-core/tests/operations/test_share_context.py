"""Kernel tests for the plan-returning ``share_context`` operation."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from nauro_core.constants import MAX_BRIEF_BYTES, MAX_QUESTION_LENGTH
from nauro_core.operations.planning import PlanRejected, canonical_payload_bytes
from nauro_core.operations.share_context import (
    SUMMARY_CHAR_LIMIT,
    ShareContextPlan,
    brief_path,
    compose_pointer_body,
    share_context,
)

SLUG = "auth-cutover"
CONTENT = "# Auth cutover\n\nThe plan in full.\n"
SUMMARY = "the auth cutover plan"


def _plan(**overrides) -> ShareContextPlan:
    args = {
        "slug": SLUG,
        "content": CONTENT,
        "pointer_kind": "brief",
        "summary": SUMMARY,
    }
    args.update(overrides)
    return share_context(**args)


class TestShareContextAccepts:
    def test_returns_frozen_plan(self) -> None:
        plan = _plan()
        assert isinstance(plan, ShareContextPlan)
        assert plan.operation == "share_context"
        assert plan.slug == SLUG
        assert plan.content == CONTENT
        assert plan.summary == SUMMARY
        with pytest.raises(ValidationError):
            plan.slug = "changed"

    def test_path_derives_only_from_slug(self) -> None:
        assert _plan().path == f"context/{SLUG}.md"
        assert brief_path("x") == "context/x.md"

    def test_content_digest_is_sha256_of_utf8_bytes(self) -> None:
        plan = _plan()
        assert plan.content_digest == hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize(
        "pointer_kind, prefix",
        [("brief", "BRIEF:"), ("resume", "RESUME:"), ("selection", "SELECT:")],
    )
    def test_pointer_body_uses_ascii_separator(self, pointer_kind: str, prefix: str) -> None:
        plan = _plan(pointer_kind=pointer_kind)
        assert plan.pointer_body == f"{prefix} context/{SLUG}.md - {SUMMARY}"
        assert "—" not in plan.pointer_body

    def test_compose_pointer_body_matches_plan(self) -> None:
        plan = _plan()
        assert compose_pointer_body("brief", plan.path, SUMMARY) == plan.pointer_body

    def test_content_at_exact_byte_cap_accepted(self) -> None:
        plan = _plan(content="x" * MAX_BRIEF_BYTES)
        assert len(plan.content.encode("utf-8")) == MAX_BRIEF_BYTES

    @pytest.mark.parametrize(
        "summary",
        [
            "the auth cutover plan",
            "the auth\u00a0cutover plan",  # non-breaking space inside real text
            "family \U0001f468\u200d\U0001f469\u200d\U0001f467 rollout",  # ZWJ sequence
            "\u8a8d\u8a3c\u5207\u308a\u66ff\u3048",  # CJK only, no ASCII
            "20260810",  # digits only
            "\u0645\u06cc\u200c\u0631\u0648\u062f",  # Persian, interior ZWNJ
        ],
    )
    def test_visible_summary_accepted(self, summary: str) -> None:
        assert _plan(summary=summary).summary == summary

    def test_worst_case_pointer_body_fits_question_length(self) -> None:
        # Maximum slug plus maximum summary: the composed pointer must fit
        # the flag_question body cap by construction, since the hosted
        # workflow appends it as an ordinary entry.
        plan = _plan(slug="a" * 120, summary="s" * SUMMARY_CHAR_LIMIT)
        assert len(plan.pointer_body) <= MAX_QUESTION_LENGTH


class TestShareContextPayloadBytes:
    def test_canonical_json_shape(self) -> None:
        plan = _plan()
        assert plan.payload_bytes == canonical_payload_bytes(
            {
                "operation": "share_context",
                "slug": SLUG,
                "content": CONTENT,
                "pointer_kind": "brief",
                "summary": SUMMARY,
            }
        )
        decoded = json.loads(plan.payload_bytes)
        assert list(decoded) == sorted(decoded)

    def test_deterministic_across_calls(self) -> None:
        assert _plan().payload_bytes == _plan().payload_bytes

    def test_pointer_kind_participates_in_payload_identity(self) -> None:
        assert _plan().payload_bytes != _plan(pointer_kind="resume").payload_bytes


class TestShareContextRejects:
    @pytest.mark.parametrize(
        "slug",
        ["", "-leading", "trailing-", "Upper", "under_score", "spa ce", "a" * 121],
    )
    def test_invalid_slug_rejects(self, slug: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(slug=slug)
        assert "slug" in excinfo.value.reason

    def test_nul_in_content_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(content="body\x00")
        assert "NUL" in excinfo.value.reason

    def test_unencodable_content_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(content="body\ud800")
        assert "UTF-8" in excinfo.value.reason

    def test_content_over_byte_cap_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(content="x" * (MAX_BRIEF_BYTES + 1))
        assert str(MAX_BRIEF_BYTES) in excinfo.value.reason

    def test_byte_cap_counts_encoded_bytes_not_characters(self) -> None:
        # é is two UTF-8 bytes: a character count under the cap can still
        # exceed it in bytes, and the cap is a byte cap.
        with pytest.raises(PlanRejected):
            _plan(content="é" * ((MAX_BRIEF_BYTES // 2) + 1))

    def test_unknown_pointer_kind_rejects_naming_vocabulary(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(pointer_kind="checkpoint")
        assert "brief" in excinfo.value.reason
        assert "resume" in excinfo.value.reason
        assert "selection" in excinfo.value.reason

    @pytest.mark.parametrize("summary", ["", "   "])
    def test_empty_summary_rejects(self, summary: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary=summary)
        assert "summary" in excinfo.value.reason

    def test_unencodable_summary_rejects(self) -> None:
        # A lone surrogate is reachable from hosted JSON input; it must be
        # a Tier-1 rejection, not an encode error inside payload composition.
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary="summary\ud800")
        assert "UTF-8" in excinfo.value.reason

    @pytest.mark.parametrize("summary", ["two\nlines", "carriage\rreturn"])
    def test_multiline_summary_rejects(self, summary: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary=summary)
        assert "single line" in excinfo.value.reason

    @pytest.mark.parametrize(
        "summary",
        [
            "tab\tseparated",
            "vertical\x0btab",
            "file\x1cseparator",
            "next\x85line",
            "line\u2028separator",
            "paragraph\u2029separator",
            "escape\x1bsequence",
        ],
    )
    def test_control_character_summary_rejects(self, summary: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary=summary)
        assert "control" in excinfo.value.reason

    @pytest.mark.parametrize(
        "summary",
        [
            "override\u202edrappa",
            "isolate\u2066text",
            "left\u200emark",
        ],
    )
    def test_bidi_control_summary_rejects(self, summary: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary=summary)
        assert "bidirectional" in excinfo.value.reason

    @pytest.mark.parametrize(
        "summary",
        [
            "\u200b\u200b\u200b",  # zero-width spaces only
            "\ufe0f\ufe0f",  # variation selectors only
            "\u0301\u0308",  # combining marks only
            "\u3164\u3164",  # hangul filler only
            "\u115f",  # hangul choseong filler only
            "\uffa0",  # halfwidth hangul filler only
        ],
    )
    def test_summary_without_visible_characters_rejects(self, summary: str) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary=summary)
        assert "visible" in excinfo.value.reason

    def test_over_length_summary_rejects(self) -> None:
        with pytest.raises(PlanRejected) as excinfo:
            _plan(summary="s" * (SUMMARY_CHAR_LIMIT + 1))
        assert str(SUMMARY_CHAR_LIMIT) in excinfo.value.reason

    def test_rejection_is_typed_value_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _plan(slug="NOPE")
        assert isinstance(excinfo.value, PlanRejected)
        assert excinfo.value.reason
