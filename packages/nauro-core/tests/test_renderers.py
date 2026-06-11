"""Tests for human-readable renderers for MCP read-tool responses.

Each renderer is a pure function: takes the result dict the read-tool
adapter produces, returns a formatted text block. The JSON envelope
itself is rendered by the dispatcher; these tests only cover the
human-formatted block.
"""

from __future__ import annotations

import json

from nauro_core.renderers import (
    render_check_decision,
    render_get_context,
    render_get_decision,
    render_list_decisions,
    render_list_projects,
    render_search_decisions,
)


class TestRenderCheckDecision:
    def test_empty_store_passes_through_assessment(self):
        """When the store has no decisions, the kernel returns its
        ``NO_DECISIONS_TO_CHECK`` assessment with an empty
        ``related_decisions`` list; the renderer surfaces that text
        verbatim."""
        result = {
            "store": "remote",
            "related_decisions": [],
            "assessment": (
                "No existing decisions to check against. Propose your decision when ready."
            ),
        }
        text = render_check_decision(result)
        assert "No existing decisions" in text

    def test_no_related_decisions_passes_through(self):
        result = {
            "store": "remote",
            "related_decisions": [],
            "assessment": "No related decisions found.",
        }
        text = render_check_decision(result)
        assert "No related decisions found" in text

    def test_single_related_decision(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-145",
                    "title": "Adopt 1-hour prompt cache tier for planner",
                    "score": 19.07,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": "Planner cache_control set to ttl: 1h.",
                }
            ],
            "assessment": (
                'Top match: D145 "Adopt 1-hour prompt cache tier for planner" '
                "(status active, decided 2026-05-10, BM25 19.1). "
                "Call get_decision(145) before proposing."
            ),
        }
        text = render_check_decision(result)
        assert "D145" in text
        assert "top match" in text
        assert "19.07" in text or "19.1" in text
        assert "Adopt 1-hour prompt cache tier" in text
        assert "Planner cache_control" in text
        # Call-to-action footer must mention get_decision
        assert "get_decision" in text

    def test_multi_hit_marks_top_and_lists_rest(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-145",
                    "title": "Adopt 1-hour prompt cache tier for planner",
                    "score": 19.07,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": (
                        "Pareto sets the planner cache_control to ttl: 1h, "
                        "paying the documented write surcharge."
                    ),
                },
                {
                    "id": "decision-058",
                    "title": (
                        "Prompt cache strategy: planner always; "
                        "executor per-session; reviewer never"
                    ),
                    "score": 18.05,
                    "status": "active",
                    "date": "2026-03-22",
                    "rationale_preview": "Cache per role.",
                },
                {
                    "id": "decision-102",
                    "title": "Retire monolithic-agent mode by 2026-Q3",
                    "score": 5.95,
                    "status": "active",
                    "date": "2026-04-01",
                    "rationale_preview": "Modes go away.",
                },
            ],
            "assessment": (
                "Found 3 related decisions. "
                'Top match: D145 "Adopt 1-hour prompt cache tier for planner" '
                "(status active, decided 2026-05-10, BM25 19.1). "
                "Call get_decision on each related decision before proposing."
            ),
        }
        text = render_check_decision(result)
        # Top match marker only on the top hit
        top_marker_count = text.count("top match")
        assert top_marker_count == 1
        # All three decision labels appear
        assert "D145" in text
        assert "D058" in text
        assert "D102" in text
        # All three titles appear
        assert "Adopt 1-hour prompt cache tier" in text
        assert "Prompt cache strategy" in text
        assert "Retire monolithic-agent mode" in text
        # Top hit shows rationale preview; lower hits do not
        assert "Pareto sets the planner" in text
        assert "Cache per role" not in text
        assert "Modes go away" not in text
        # Footer guidance
        assert "get_decision" in text

    def test_long_title_does_not_blow_up(self):
        long_title = "x" * 200
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-001",
                    "title": long_title,
                    "score": 5.0,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": "",
                }
            ],
            "assessment": "...",
        }
        text = render_check_decision(result)
        # Renderer must not crash; the title should appear in some form.
        assert "D001" in text

    def test_unicode_in_title(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-007",
                    "title": "Adopt café-style 設計 patterns",
                    "score": 3.14,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": "あいうえお",
                }
            ],
            "assessment": "...",
        }
        text = render_check_decision(result)
        assert "café" in text
        assert "設計" in text

    def test_missing_optional_fields_does_not_crash(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-001",
                    "title": "A",
                    "score": 1.0,
                    "status": "active",
                    "date": "",
                    "rationale_preview": "",
                }
            ],
            "assessment": "...",
        }
        text = render_check_decision(result)
        assert "D001" in text

    def test_error_path_emits_error_header(self):
        result = {"store": "remote", "error": "Proposed approach exceeds 8000 chars"}
        text = render_check_decision(result)
        assert text.startswith("Error:")
        assert "Proposed approach exceeds" in text


class TestRenderGetDecision:
    def test_content_passthrough_with_header(self):
        body = (
            "---\n"
            "date: 2026-05-10\n"
            "status: active\n"
            "---\n"
            "\n"
            "# 145 — Adopt 1-hour prompt cache tier for planner\n"
            "\n"
            "## Decision\nFoo.\n"
        )
        result = {"store": "remote", "content": body}
        text = render_get_decision(result)
        # Body must be present
        assert "Foo." in text
        # Title is surfaced as a header line for chat clients
        assert "145" in text
        assert "Adopt 1-hour prompt cache tier" in text

    def test_error_path(self):
        result = {"store": "remote", "error": "Decision 999 not found"}
        text = render_get_decision(result)
        assert text.startswith("Error:")
        assert "Decision 999 not found" in text

    def test_full_mode_default_matches_explicit_full(self):
        body = (
            "---\n"
            "date: 2026-05-10\n"
            "status: active\n"
            "---\n"
            "\n"
            "# 145 — Adopt 1-hour prompt cache tier for planner\n"
            "\n"
            "## Decision\nFoo.\n"
        )
        result = {"store": "remote", "content": body}
        assert render_get_decision(result) == render_get_decision(result, mode="full")

    def test_header_mode_emits_projection_as_sole_block(self):
        """Header mode surfaces the kernel's compact projection verbatim — no
        second title header layered on top of the projection's own title."""
        projection = (
            "status: active\n"
            "supersedes: 190\n"
            "date: 2026-05-28\n"
            "decision_type: api_design\n"
            "confidence: medium\n"
            "\n"
            "# 246 — Header projection mode\n"
            "\n"
            "Triage frontmatter plus a short lede."
        )
        result = {"store": "remote", "content": projection}
        text = render_get_decision(result, mode="header")
        assert text == projection
        # The projection's title appears exactly once (no duplicate header).
        assert text.count("# 246 — Header projection mode") == 1

    def test_header_mode_error_path(self):
        result = {"store": "remote", "error": "Decision 999 not found"}
        text = render_get_decision(result, mode="header")
        assert text.startswith("Error:")
        assert "Decision 999 not found" in text


class TestRenderSearchDecisions:
    def test_empty_results(self):
        result = {
            "store": "remote",
            "results": [],
            "total_matches": 0,
            "truncated": False,
            "query": "nothing matches",
        }
        text = render_search_decisions(result)
        assert "No matches" in text or "no matches" in text
        assert "nothing matches" in text

    def test_single_hit(self):
        result = {
            "store": "remote",
            "results": [
                {
                    "number": 145,
                    "title": "Adopt 1-hour prompt cache tier for planner",
                    "date": "2026-05-10",
                    "status": "active",
                    "relevance_snippet": "Planner cache_control set to ttl: 1h.",
                    "score": 19.07,
                }
            ],
            "total_matches": 1,
            "truncated": False,
            "query": "prompt cache",
        }
        text = render_search_decisions(result)
        assert "D145" in text
        assert "Adopt 1-hour prompt cache tier" in text
        assert "Planner cache_control" in text
        assert "19.07" in text or "19.1" in text
        assert "prompt cache" in text

    def test_truncated_flag_surfaces(self):
        result = {
            "store": "remote",
            "results": [
                {
                    "number": n,
                    "title": f"Decision {n}",
                    "date": "2026-05-10",
                    "status": "active",
                    "relevance_snippet": "snippet",
                    "score": 10.0 - n,
                }
                for n in range(1, 6)
            ],
            "total_matches": 5,
            "truncated": True,
            "query": "something",
        }
        text = render_search_decisions(result)
        for n in range(1, 6):
            assert f"D00{n}" in text
        assert "truncated" in text.lower() or "more" in text.lower()

    def test_error_path(self):
        result = {"store": "remote", "error": "Query must be non-empty"}
        text = render_search_decisions(result)
        assert text.startswith("Error:")
        assert "non-empty" in text

    def _local_envelope(self):
        """A local-transport envelope: no echoed ``query`` key (kernel prune)."""
        return {
            "store": "local",
            "results": [
                {
                    "number": 7,
                    "title": "Adopt BM25 ranking",
                    "date": "2026-05-10",
                    "status": "active",
                    "relevance_snippet": "snippet",
                    "score": 9.0,
                }
            ],
        }

    def test_query_kwarg_renders_header_without_dict_key(self):
        # The local envelope carries no "query" key; the kwarg must supply it.
        text = render_search_decisions(self._local_envelope(), query="lexical ranking")
        assert 'for "lexical ranking"' in text

    def test_missing_query_falls_back_to_empty_header(self):
        # Without the kwarg and without a dict key, the header degrades to the
        # empty string (the pre-fix local behavior) rather than raising.
        text = render_search_decisions(self._local_envelope())
        assert 'for ""' in text

    def test_query_kwarg_takes_precedence_over_dict_key(self):
        result = {**self._local_envelope(), "query": "dict-query"}
        text = render_search_decisions(result, query="kwarg-query")
        assert 'for "kwarg-query"' in text
        assert "dict-query" not in text

    def test_dict_query_still_renders_when_no_kwarg(self):
        # Backward compatibility: the remote calls renderer(result) positionally
        # and relies on the echoed "query" key in its wire envelope.
        result = {**self._local_envelope(), "query": "remote-query"}
        text = render_search_decisions(result)
        assert 'for "remote-query"' in text


class TestRenderListDecisions:
    def test_empty_returns_guidance(self):
        result = {
            "store": "remote",
            "decisions": [],
            "total": 0,
            "truncated": False,
            "guidance": (
                "No decisions recorded yet for this project.\n"
                "\n"
                "Use propose_decision to record your first..."
            ),
        }
        text = render_list_decisions(result)
        # Empty-state guidance flows through unchanged.
        assert "No decisions recorded yet" in text

    def test_lists_decisions(self):
        result = {
            "store": "remote",
            "decisions": [
                {
                    "number": 42,
                    "title": "Use JSON-RPC transport",
                    "date": "2026-03-14",
                    "status": "active",
                    "type": "api_design",
                    "confidence": "high",
                },
                {
                    "number": 2,
                    "title": "Chose FastAPI",
                    "date": "2026-03-12",
                    "status": "active",
                    "type": "pattern",
                    "confidence": "high",
                },
            ],
            "total": 2,
            "truncated": False,
        }
        text = render_list_decisions(result)
        assert "D042" in text
        assert "D002" in text
        assert "Use JSON-RPC transport" in text
        assert "Chose FastAPI" in text

    def test_truncated_surfaces(self):
        result = {
            "store": "remote",
            "decisions": [
                {
                    "number": n,
                    "title": f"Decision {n}",
                    "date": "2026-03-14",
                    "status": "active",
                    "confidence": "high",
                }
                for n in range(50, 30, -1)
            ],
            "total": 100,
            "truncated": True,
        }
        text = render_list_decisions(result)
        assert "truncated" in text.lower() or "more" in text.lower() or "100" in text

    def test_long_title_renders(self):
        long_title = "Very long decision title " * 10
        result = {
            "store": "remote",
            "decisions": [
                {
                    "number": 1,
                    "title": long_title.strip(),
                    "date": "2026-03-14",
                    "status": "active",
                    "confidence": "high",
                }
            ],
            "total": 1,
            "truncated": False,
        }
        text = render_list_decisions(result)
        assert "D001" in text

    def test_error_path(self):
        result = {"store": "remote", "error": "List decisions failed"}
        text = render_list_decisions(result)
        assert text.startswith("Error:")


class TestRenderGetContext:
    def test_passthrough_when_present(self):
        result = {
            "store": "remote",
            "context": (
                "# Current State\n\nWorking on MCP renderer.\n\n## Recent Decisions\n- D215\n"
            ),
        }
        text = render_get_context(result)
        assert "Current State" in text
        assert "Recent Decisions" in text

    def test_empty_state_passthrough(self):
        # Representative empty-state guidance. Both the remote MCP server
        # (mcp_server.onboarding.NO_CONTEXT_YET) and the local CLI
        # (nauro.onboarding.NO_CONTEXT_YET) emit text that satisfies the
        # marker assertions below; the renderer is a passthrough so the
        # exact upstream wording is not under test here.
        empty_state = (
            "This project has no context data yet.\n\n"
            "Use propose_decision or update_state to record context directly here."
        )
        result = {"store": "remote", "context": empty_state}
        text = render_get_context(result)
        # Empty-state guidance comes through unchanged.
        assert "no context" in text.lower() or "propose_decision" in text

    def test_local_envelope_content_key(self):
        """The local stdio surface keys the assembled markdown under
        ``content`` (matching the kernel ``GetContextResult.content``
        field), while the remote MCP server keys it under ``context``.
        The shared renderer must accept either so both transports can
        wire the same registry."""
        result = {
            "store": "local",
            "content": "# Current State\n\nLocal envelope path.\n",
        }
        text = render_get_context(result)
        assert "Current State" in text
        assert "Local envelope path" in text

    def test_error_path(self):
        result = {"store": "remote", "error": "Invalid level: L9"}
        text = render_get_context(result)
        assert text.startswith("Error:")
        assert "Invalid level" in text


class TestRenderListProjects:
    def test_empty_state(self):
        result = {"projects": []}
        text = render_list_projects(result)
        assert "no projects" in text.lower() or "No projects" in text

    def test_lists_projects(self):
        result = {
            "projects": [
                {
                    "project_id": "01KQ6AZGNA0B3QBF67NBXP3S45",
                    "name": "nauro",
                    "role": "owner",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "project_id": "01KREWKMPDW2EVR66F9XXNERGB",
                    "name": "throwaway-supersede-1778616226",
                    "role": "member",
                    "created_at": "2026-04-01T00:00:00+00:00",
                },
            ]
        }
        text = render_list_projects(result)
        assert "nauro" in text
        assert "throwaway-supersede" in text
        assert "01KQ6AZGNA0B3QBF67NBXP3S45" in text
        assert "01KREWKMPDW2EVR66F9XXNERGB" in text
        assert "owner" in text
        assert "member" in text


class TestRendererPurity:
    """Renderers must be pure functions of the result dict.

    They cannot read S3, hit DDB, or import anything that would. Each call
    must be deterministic for a given input.
    """

    def test_same_input_same_output(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-001",
                    "title": "A decision",
                    "score": 1.0,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": "Rationale.",
                }
            ],
            "assessment": "...",
        }
        first = render_check_decision(result)
        second = render_check_decision(result)
        assert first == second

    def test_result_dict_not_mutated(self):
        result = {
            "store": "remote",
            "related_decisions": [
                {
                    "id": "decision-001",
                    "title": "A decision",
                    "score": 1.0,
                    "status": "active",
                    "date": "2026-05-10",
                    "rationale_preview": "Rationale.",
                }
            ],
            "assessment": "...",
        }
        snapshot = json.dumps(result, sort_keys=True)
        render_check_decision(result)
        assert json.dumps(result, sort_keys=True) == snapshot


class TestNoProjectGuidance:
    """The local stdio server emits ``{"status": "error", "guidance": ...}``
    when no project resolves (e.g. a read tool called at session start in a
    repo that has not run ``nauro init``). That envelope has no ``error`` key
    and no payload key, so every read renderer must surface the guidance text
    rather than returning an empty string or a misleading "no results" line.
    """

    GUIDANCE = (
        "Welcome to Nauro! No project store found.\n\n"
        "To get started:\n1. Run: nauro init <project-name>\n"
    )

    def _envelope(self) -> dict:
        return {"store": "local", "status": "error", "guidance": self.GUIDANCE}

    def test_get_context_surfaces_guidance(self):
        # The session-start tool must not return an empty content block.
        text = render_get_context(self._envelope())
        assert "Welcome to Nauro" in text
        assert text != ""

    def test_get_decision_surfaces_guidance(self):
        text = render_get_decision(self._envelope())
        assert "Welcome to Nauro" in text

    def test_check_decision_surfaces_guidance_not_false_clear(self):
        # Must NOT report a false "No related decisions found." — that would
        # tell the agent the history is clear when no store was read.
        text = render_check_decision(self._envelope())
        assert "Welcome to Nauro" in text
        assert "No related decisions found" not in text

    def test_search_decisions_surfaces_guidance_not_false_empty(self):
        text = render_search_decisions(self._envelope())
        assert "Welcome to Nauro" in text
        assert "No matches" not in text

    def test_list_decisions_still_surfaces_guidance(self):
        text = render_list_decisions(self._envelope())
        assert "Welcome to Nauro" in text

    def test_remote_empty_store_guidance_unaffected(self):
        # The remote empty-store path (decisions empty + guidance, no
        # status:error) must keep flowing through unchanged.
        result = {
            "store": "remote",
            "decisions": [],
            "guidance": "No decisions recorded yet for this project.",
        }
        assert "No decisions recorded yet" in render_list_decisions(result)
