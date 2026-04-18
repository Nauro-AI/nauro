"""Eval suite for nauro.extraction pipeline and prompts.

Test cases are structured as fixtures that can run through the real LLM
pipeline. Unit tests validate prompt structure, test case format, pipeline
routing, and graceful failure handling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nauro.extraction.pipeline import (
    EXTRACTION_TOOL,
    extract_from_commit,
    process_commit,
)
from nauro.extraction.prompts import (
    COMPACTION_EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    build_compaction_extraction_prompt,
    build_extraction_user_prompt,
)
from nauro.extraction.signal import (
    SignalScore,
    compute_composite,
    from_dict,
    should_extract,
)
from nauro.extraction.types import ExtractionResult, ExtractionSkipped
from nauro.templates.scaffolds import scaffold_project_store

# ---------------------------------------------------------------------------
# Test case schema
# ---------------------------------------------------------------------------


@dataclass
class ExtractionTestCase:
    """A single eval case for the extraction pipeline."""

    name: str
    commit_message: str
    diff_summary: str
    changed_files: list[str]
    expected_skip: bool
    expected_signal_range: tuple[float, float]
    expected_has_decisions: bool
    expected_has_questions: bool = False
    expected_has_state_delta: bool | None = None  # None = don't assert


# ---------------------------------------------------------------------------
# Load test cases from both inline and JSON fixture
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture_cases() -> list[ExtractionTestCase]:
    """Load extraction test cases from fixtures/extraction_cases.json."""
    fixture_path = FIXTURES_DIR / "extraction_cases.json"
    if not fixture_path.exists():
        return []
    raw = json.loads(fixture_path.read_text())
    cases = []
    for item in raw:
        cases.append(
            ExtractionTestCase(
                name=item["name"],
                commit_message=item["commit_message"],
                diff_summary=item["diff_summary"],
                changed_files=item["changed_files"],
                expected_skip=item["expected_skip"],
                expected_signal_range=tuple(item["expected_signal_range"]),
                expected_has_decisions=item["expected_has_decisions"],
                expected_has_questions=item.get("expected_has_questions", False),
                expected_has_state_delta=item.get("expected_has_state_delta"),
            )
        )
    return cases


# Inline eval cases (the authoritative set)
EXTRACTION_EVAL_CASES: list[ExtractionTestCase] = [
    # -----------------------------------------------------------------------
    # HIGH SIGNAL — should extract decisions and/or state delta
    # -----------------------------------------------------------------------
    ExtractionTestCase(
        name="task_queue_migration",
        commit_message="migrate from arq+Redis to procrastinate for capsule state machine",
        diff_summary="""\
- from arq import create_pool
+ from procrastinate import App, ProcrastinateEngine
- REDIS_URL = settings.REDIS_URL
+ engine = ProcrastinateEngine(conninfo=settings.DATABASE_URL)
- async def enqueue_capsule_transition(capsule_id, target_state):
-     pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
-     await pool.enqueue_job("transition", capsule_id, target_state)
+ @app.task
+ async def transition_capsule(capsule_id: int, target_state: str):
+     async with engine:
+         await run_transition(capsule_id, target_state)""",
        changed_files=[
            "src/capsules/tasks.py",
            "src/capsules/worker.py",
            "pyproject.toml",
            "docker-compose.yml",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 1.0),
        expected_has_decisions=True,
    ),
    ExtractionTestCase(
        name="deployment_platform_switch",
        commit_message="add Fly.io deployment config, remove Railway references",
        diff_summary="""\
- [railway]
-   builder = "nixpacks"
-   startCommand = "uvicorn app:main"
+ # fly.toml
+ app = "nauro-api"
+ primary_region = "ams"
+ [http_service]
+   internal_port = 8080
+   force_https = true
+ [deploy]
+   strategy = "rolling"
- RAILWAY_TOKEN in .env.example
+ FLY_API_TOKEN in .env.example""",
        changed_files=[
            "fly.toml",
            "Dockerfile",
            ".env.example",
            "railway.toml",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 1.0),
        expected_has_decisions=True,
    ),
    ExtractionTestCase(
        name="new_endpoint",
        commit_message="implement capsule contribution creation endpoint",
        diff_summary="""\
+ @router.post("/capsules/{capsule_id}/contributions")
+ async def create_contribution(
+     capsule_id: int,
+     body: ContributionCreate,
+     user: User = Depends(get_current_user),
+ ) -> ContributionResponse:
+     contribution = await ContributionService.create(
+         capsule_id=capsule_id,
+         author_id=user.id,
+         content=body.content,
+         kind=body.kind,
+     )
+     return ContributionResponse.from_orm(contribution)""",
        changed_files=[
            "src/api/routes/contributions.py",
            "src/api/schemas/contributions.py",
            "src/services/contributions.py",
        ],
        expected_skip=False,
        expected_signal_range=(0.0, 1.0),
        expected_has_decisions=False,
        expected_has_state_delta=None,
    ),
    ExtractionTestCase(
        name="protocol_switch",
        commit_message="switch from REST to WebSocket for real-time capsule updates",
        diff_summary="""\
- @router.get("/capsules/{id}/updates")
- async def poll_updates(id: int, since: datetime):
-     return await UpdateService.get_since(id, since)
+ @router.websocket("/capsules/{id}/ws")
+ async def capsule_ws(websocket: WebSocket, id: int):
+     await websocket.accept()
+     async for update in UpdateService.subscribe(id):
+         await websocket.send_json(update.dict())
+ class UpdateService:
+     @staticmethod
+     async def subscribe(capsule_id: int):
+         async with broadcast.subscribe(f"capsule:{capsule_id}") as sub:
+             async for event in sub:
+                 yield event""",
        changed_files=[
            "src/api/routes/updates.py",
            "src/services/updates.py",
            "src/core/broadcast.py",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 1.0),
        expected_has_decisions=True,
    ),
    ExtractionTestCase(
        name="rls_policies",
        commit_message="add Supabase RLS policies for group membership",
        diff_summary="""\
+ CREATE POLICY "group_members_select" ON group_members
+   FOR SELECT USING (
+     auth.uid() = user_id
+     OR EXISTS (
+       SELECT 1 FROM group_members gm
+       WHERE gm.group_id = group_members.group_id
+       AND gm.user_id = auth.uid()
+       AND gm.role IN ('admin', 'owner')
+     )
+   );
+ CREATE POLICY "group_members_insert" ON group_members
+   FOR INSERT WITH CHECK (
+     EXISTS (
+       SELECT 1 FROM group_members
+       WHERE group_id = NEW.group_id
+       AND user_id = auth.uid()
+       AND role IN ('admin', 'owner')
+     )
+   );
+ ALTER TABLE group_members ENABLE ROW LEVEL SECURITY;""",
        changed_files=[
            "supabase/migrations/20240115_group_rls.sql",
        ],
        expected_skip=False,
        expected_signal_range=(0.4, 1.0),
        expected_has_decisions=True,
    ),
    # -----------------------------------------------------------------------
    # MEDIUM SIGNAL — state delta, maybe decisions
    # -----------------------------------------------------------------------
    ExtractionTestCase(
        name="reader_writer_refactor",
        commit_message="refactor store module into reader/writer split",
        diff_summary="""\
- # store.py — all store operations
- def read_project(name): ...
- def write_decision(name, decision): ...
+ # reader.py
+ def read_project(name): ...
+ def read_decisions(name): ...
+ # writer.py
+ def write_decision(name, decision): ...
+ def write_state(name, state): ...""",
        changed_files=[
            "src/nauro/store/reader.py",
            "src/nauro/store/writer.py",
        ],
        expected_skip=False,
        expected_signal_range=(0.2, 0.7),
        expected_has_decisions=False,
        expected_has_state_delta=True,
    ),
    ExtractionTestCase(
        name="update_claude_md",
        commit_message="update CLAUDE.md with new API conventions",
        diff_summary="""\
+ ## API conventions
+ - All endpoints return JSON with a top-level `data` key
+ - Error responses use RFC 7807 Problem Details
+ - Pagination via cursor, not offset""",
        changed_files=["CLAUDE.md"],
        expected_skip=False,
        expected_signal_range=(0.0, 0.7),
        expected_has_decisions=False,
        expected_has_state_delta=None,
    ),
    ExtractionTestCase(
        name="mcp_error_handling",
        commit_message="add error handling for MCP connection failures",
        diff_summary="""\
+ try:
+     response = await client.send(request)
+ except ConnectionError:
+     logger.warning("MCP server unreachable, returning cached context")
+     return cached_context
+ except TimeoutError:
+     logger.warning("MCP request timed out after %ds", TIMEOUT)
+     return cached_context""",
        changed_files=["src/nauro/mcp/client.py"],
        expected_skip=False,
        expected_signal_range=(0.2, 0.7),
        expected_has_decisions=False,
        expected_has_state_delta=True,
    ),
    ExtractionTestCase(
        name="todo_in_commit",
        commit_message="add initial capsule archive flow — TODO: decide on soft vs hard delete",
        diff_summary="""\
+ async def archive_capsule(capsule_id: int):
+     # TODO: should this be soft delete (status=archived) or hard delete?
+     # For now using soft delete, but storage costs may force hard delete later
+     await db.execute(
+         "UPDATE capsules SET status = 'archived' WHERE id = :id",
+         {"id": capsule_id},
+     )""",
        changed_files=["src/services/capsules.py"],
        expected_skip=False,
        expected_signal_range=(0.3, 0.8),
        expected_has_decisions=False,
        expected_has_questions=True,
        expected_has_state_delta=True,
    ),
    # -----------------------------------------------------------------------
    # LOW SIGNAL — should skip
    # -----------------------------------------------------------------------
    ExtractionTestCase(
        name="typo_fix",
        commit_message="fix typo in README",
        diff_summary="""\
- Nauro is a local CLI + MCP server that mainains versioned project context
+ Nauro is a local CLI + MCP server that maintains versioned project context""",
        changed_files=["README.md"],
        expected_skip=True,
        expected_signal_range=(0.0, 0.2),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="dependency_bump",
        commit_message="bump anthropic SDK to 0.40.0",
        diff_summary="""\
- anthropic = "^0.39.0"
+ anthropic = "^0.40.0"  """,
        changed_files=["pyproject.toml"],
        expected_skip=True,
        expected_signal_range=(0.0, 0.2),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="formatting_run",
        commit_message="run ruff format",
        diff_summary="""\
- def foo(  x,y,z   ):
+ def foo(x, y, z):
-     return x+y+z
+     return x + y + z""",
        changed_files=[
            "src/nauro/cli/main.py",
            "src/nauro/store/reader.py",
            "src/nauro/store/writer.py",
        ],
        expected_skip=True,
        expected_signal_range=(0.0, 0.15),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="merge_commit",
        commit_message="merge branch 'feature/init' into main",
        diff_summary="",
        changed_files=[],
        expected_skip=True,
        expected_signal_range=(0.0, 0.15),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="lockfile_update",
        commit_message="update poetry.lock",
        diff_summary="(binary/generated lockfile content)",
        changed_files=["poetry.lock"],
        expected_skip=True,
        expected_signal_range=(0.0, 0.15),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="linting_fixes",
        commit_message="fix linting errors",
        diff_summary="""\
- import os, sys
+ import os
+ import sys
- x = dict['key']
+ x = dict["key"]""",
        changed_files=[
            "src/nauro/cli/main.py",
            "src/nauro/extraction/pipeline.py",
        ],
        expected_skip=True,
        expected_signal_range=(0.0, 0.2),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="gitignore_additions",
        commit_message="add .gitignore entries",
        diff_summary="""\
+ .nauro/
+ *.pyc
+ __pycache__/
+ .env""",
        changed_files=[".gitignore"],
        expected_skip=True,
        expected_signal_range=(0.0, 0.2),
        expected_has_decisions=False,
    ),
    ExtractionTestCase(
        name="ci_pipeline_setup",
        commit_message="add GitHub Actions CI pipeline with test and lint jobs",
        diff_summary="""\
+ name: CI
+ on: [push, pull_request]
+ jobs:
+   test:
+     runs-on: ubuntu-latest
+     steps:
+       - uses: actions/checkout@v4
+       - uses: actions/setup-python@v5
+         with: { python-version: "3.11" }
+       - run: pip install -e ".[dev]"
+       - run: pytest
+   lint:
+     runs-on: ubuntu-latest
+     steps:
+       - uses: actions/checkout@v4
+       - run: pip install ruff
+       - run: ruff check .""",
        changed_files=[".github/workflows/ci.yml"],
        expected_skip=False,
        expected_signal_range=(0.0, 0.8),
        expected_has_decisions=False,
        expected_has_state_delta=None,
    ),
    ExtractionTestCase(
        name="auth_middleware_rewrite",
        commit_message="replace custom JWT validation with supabase-py auth, drop pyjwt",
        diff_summary="""\
- import jwt
- def verify_token(token: str) -> dict:
-     return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
+ from supabase import create_client
+ supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
+ async def get_current_user(request: Request) -> User:
+     token = request.headers.get("Authorization", "").removeprefix("Bearer ")
+     resp = supabase.auth.get_user(token)
+     if not resp.user:
+         raise HTTPException(401)
+     return User(id=resp.user.id, email=resp.user.email)
- pyjwt in pyproject.toml
+ supabase-py in pyproject.toml""",
        changed_files=[
            "src/auth/middleware.py",
            "pyproject.toml",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 1.0),
        expected_has_decisions=True,
    ),
]


# ---------------------------------------------------------------------------
# Prompt structure tests
# ---------------------------------------------------------------------------


class TestPromptStructure:
    """Validate the extraction prompt templates are well-formed."""

    def test_system_prompt_contains_json_schema(self):
        assert '"decisions"' in EXTRACTION_SYSTEM_PROMPT
        assert '"signal"' in EXTRACTION_SYSTEM_PROMPT
        assert '"composite_score"' in EXTRACTION_SYSTEM_PROMPT
        assert '"skip"' in EXTRACTION_SYSTEM_PROMPT
        assert '"state_delta"' in EXTRACTION_SYSTEM_PROMPT
        assert '"questions"' in EXTRACTION_SYSTEM_PROMPT
        assert '"reasoning"' in EXTRACTION_SYSTEM_PROMPT

    def test_system_prompt_contains_signal_dimensions(self):
        assert "architectural_significance" in EXTRACTION_SYSTEM_PROMPT
        assert "novelty" in EXTRACTION_SYSTEM_PROMPT
        assert "rationale_density" in EXTRACTION_SYSTEM_PROMPT
        assert "reversibility" in EXTRACTION_SYSTEM_PROMPT
        assert "scope" in EXTRACTION_SYSTEM_PROMPT

    def test_system_prompt_contains_scoring_guidance(self):
        assert "decision_type" in EXTRACTION_SYSTEM_PROMPT
        assert "architecture" in EXTRACTION_SYSTEM_PROMPT
        assert "library_choice" in EXTRACTION_SYSTEM_PROMPT

    def test_system_prompt_mentions_skip_criteria(self):
        for keyword in ["formatting", "typo", "lockfile", "merge commit"]:
            assert keyword.lower() in EXTRACTION_SYSTEM_PROMPT.lower(), (
                f"System prompt should mention skip criterion: {keyword}"
            )

    def test_compaction_prompt_exists(self):
        assert "compaction" in COMPACTION_EXTRACTION_SYSTEM_PROMPT.lower()
        assert '"decisions"' in COMPACTION_EXTRACTION_SYSTEM_PROMPT
        assert '"signal"' in COMPACTION_EXTRACTION_SYSTEM_PROMPT

    def test_build_user_prompt_returns_string(self):
        result = build_extraction_user_prompt(
            commit_message="test commit",
            diff_summary="+ added line",
            changed_files=["file.py"],
        )
        assert isinstance(result, str)

    def test_build_user_prompt_contains_all_inputs(self):
        msg = "add new feature"
        diff = "+ def new_feature(): pass"
        files = ["src/feature.py", "tests/test_feature.py"]
        result = build_extraction_user_prompt(msg, diff, files)
        assert msg in result
        assert diff in result
        for f in files:
            assert f in result

    def test_build_user_prompt_formats_files_as_list(self):
        result = build_extraction_user_prompt("msg", "diff", ["a.py", "b.py", "c.py"])
        assert "  - a.py" in result
        assert "  - b.py" in result
        assert "  - c.py" in result

    def test_build_user_prompt_handles_empty_files(self):
        result = build_extraction_user_prompt("msg", "diff", [])
        assert isinstance(result, str)
        assert "msg" in result

    def test_build_compaction_prompt(self):
        result = build_compaction_extraction_prompt("Session summary here")
        assert "Session summary here" in result
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------


class TestExtractionToolSchema:
    """Validate the tool definition used for structured output."""

    def test_tool_has_required_fields(self):
        schema = EXTRACTION_TOOL["input_schema"]
        assert set(schema["required"]) == {
            "decisions",
            "questions",
            "state_delta",
            "signal",
            "composite_score",
            "skip",
            "reasoning",
        }

    def test_tool_name(self):
        assert EXTRACTION_TOOL["name"] == "record_extraction"

    def test_decisions_schema(self):
        props = EXTRACTION_TOOL["input_schema"]["properties"]
        assert props["decisions"]["type"] == "array"
        decision_props = props["decisions"]["items"]["properties"]
        assert "title" in decision_props
        assert "rationale" in decision_props
        assert "rejected" in decision_props
        assert "confidence" in decision_props
        assert "decision_type" in decision_props
        assert "reversibility" in decision_props
        assert "files_affected" in decision_props

    def test_signal_schema(self):
        props = EXTRACTION_TOOL["input_schema"]["properties"]
        signal_props = props["signal"]["properties"]
        assert "architectural_significance" in signal_props
        assert "novelty" in signal_props
        assert "rationale_density" in signal_props
        assert "reversibility" in signal_props
        assert "scope" in signal_props

    def test_rejected_alternatives_schema(self):
        """Rejected alternatives now use object format with alternative+reason."""
        props = EXTRACTION_TOOL["input_schema"]["properties"]
        rejected_items = props["decisions"]["items"]["properties"]["rejected"]["items"]
        assert rejected_items["type"] == "object"
        assert "alternative" in rejected_items["properties"]
        assert "reason" in rejected_items["properties"]


# ---------------------------------------------------------------------------
# Signal score tests
# ---------------------------------------------------------------------------


class TestSignalScore:
    """Test the multi-dimensional signal scoring system."""

    def test_compute_composite_all_zeros(self):
        signal = SignalScore()
        assert compute_composite(signal) == 0.0

    def test_compute_composite_all_ones(self):
        signal = SignalScore(
            architectural_significance=1.0,
            novelty=1.0,
            rationale_density=1.0,
            reversibility=1.0,
            scope=1.0,
        )
        assert compute_composite(signal) == pytest.approx(1.0)

    def test_compute_composite_weighted(self):
        signal = SignalScore(
            architectural_significance=1.0,
            novelty=0.0,
            rationale_density=0.0,
            reversibility=0.0,
            scope=0.0,
        )
        assert compute_composite(signal) == pytest.approx(0.3)

    def test_compute_composite_clamped(self):
        signal = SignalScore(
            architectural_significance=2.0,
            novelty=2.0,
            rationale_density=2.0,
            reversibility=2.0,
            scope=2.0,
        )
        assert compute_composite(signal) == 1.0

    def test_should_extract_above_threshold(self):
        signal = SignalScore(composite_score=0.6)
        assert should_extract(signal, threshold=0.4) is True

    def test_should_extract_below_threshold(self):
        signal = SignalScore(composite_score=0.3)
        assert should_extract(signal, threshold=0.4) is False

    def test_should_extract_env_threshold(self, monkeypatch):
        monkeypatch.setenv("NAURO_SIGNAL_THRESHOLD", "0.8")
        signal = SignalScore(composite_score=0.6)
        assert should_extract(signal) is False

    def test_from_dict(self):
        data = {
            "signal": {
                "architectural_significance": 0.8,
                "novelty": 0.6,
                "rationale_density": 0.7,
                "reversibility": 0.9,
                "scope": 0.4,
            },
            "composite_score": 0.72,
            "reasoning": "Major architecture change",
        }
        signal = from_dict(data)
        assert signal.architectural_significance == 0.8
        assert signal.novelty == 0.6
        assert signal.composite_score == 0.72
        assert signal.reasoning == "Major architecture change"

    def test_from_dict_missing_fields(self):
        signal = from_dict({})
        assert signal.architectural_significance == 0.0
        assert signal.composite_score == 0.0
        assert signal.reasoning == ""

    def test_to_dict(self):
        signal = SignalScore(
            architectural_significance=0.5,
            novelty=0.3,
            rationale_density=0.7,
            reversibility=0.2,
            scope=0.8,
        )
        d = signal.to_dict()
        assert d["architectural_significance"] == 0.5
        assert d["scope"] == 0.8
        assert "composite_score" not in d  # Only dimensions, not computed fields

    def test_db_migration_high_arch_and_reversibility(self):
        """A database migration should score high on architectural_significance
        and reversibility."""
        signal = SignalScore(
            architectural_significance=0.9,
            novelty=0.5,
            rationale_density=0.3,
            reversibility=0.9,
            scope=0.6,
        )
        signal.composite_score = compute_composite(signal)
        assert signal.composite_score > 0.6
        assert signal.architectural_significance >= 0.8
        assert signal.reversibility >= 0.8

    def test_cross_cutting_refactor_high_scope(self):
        """A cross-cutting refactor touching 15 files should score high on scope."""
        signal = SignalScore(
            architectural_significance=0.4,
            novelty=0.2,
            rationale_density=0.3,
            reversibility=0.3,
            scope=0.9,
        )
        signal.composite_score = compute_composite(signal)
        assert signal.scope >= 0.8

    def test_reasoning_always_populated(self):
        """The reasoning field must be non-empty for real results."""
        signal = SignalScore(reasoning="This is a database schema change")
        assert signal.reasoning


# ---------------------------------------------------------------------------
# Eval case format validation
# ---------------------------------------------------------------------------


class TestEvalCaseFormat:
    """Validate that all eval cases are well-formed."""

    def test_minimum_case_count(self):
        assert len(EXTRACTION_EVAL_CASES) >= 15

    def test_signal_range_bounds(self):
        for case in EXTRACTION_EVAL_CASES:
            lo, hi = case.expected_signal_range
            assert 0.0 <= lo <= hi <= 1.0, f"{case.name}: invalid signal range ({lo}, {hi})"

    def test_skip_cases_expect_no_decisions(self):
        for case in EXTRACTION_EVAL_CASES:
            if case.expected_skip:
                assert not case.expected_has_decisions, (
                    f"{case.name}: skip=True but expected_has_decisions=True"
                )

    def test_skip_cases_have_low_signal(self):
        for case in EXTRACTION_EVAL_CASES:
            if case.expected_skip:
                _, hi = case.expected_signal_range
                assert hi <= 0.3, f"{case.name}: skip=True but signal upper bound is {hi}"

    def test_all_cases_have_commit_message(self):
        for case in EXTRACTION_EVAL_CASES:
            assert case.commit_message.strip(), f"{case.name}: empty commit message"

    def test_all_cases_have_unique_names(self):
        names = [c.name for c in EXTRACTION_EVAL_CASES]
        assert len(names) == len(set(names)), "Duplicate case names found"

    def test_high_signal_cases_exist(self):
        high = [c for c in EXTRACTION_EVAL_CASES if c.expected_signal_range[0] >= 0.4]
        assert len(high) >= 5, f"Expected >=5 high-signal cases, got {len(high)}"

    def test_medium_signal_cases_exist(self):
        medium = [
            c
            for c in EXTRACTION_EVAL_CASES
            if 0.2 <= c.expected_signal_range[0] < 0.5 and not c.expected_skip
        ]
        assert len(medium) >= 3, f"Expected >=3 medium-signal cases, got {len(medium)}"

    def test_skip_cases_exist(self):
        skips = [c for c in EXTRACTION_EVAL_CASES if c.expected_skip]
        assert len(skips) >= 5, f"Expected >=5 skip cases, got {len(skips)}"

    def test_cases_with_decisions_have_high_or_medium_signal(self):
        for case in EXTRACTION_EVAL_CASES:
            if case.expected_has_decisions:
                _, hi = case.expected_signal_range
                assert hi >= 0.5, f"{case.name}: has decisions but signal max is only {hi}"


# ---------------------------------------------------------------------------
# Fixture-loaded cases validation
# ---------------------------------------------------------------------------


class TestFixtureCases:
    """Validate that the JSON fixture loads and matches the inline cases."""

    def test_fixture_file_loads(self):
        cases = _load_fixture_cases()
        assert len(cases) > 0

    def test_fixture_cases_are_well_formed(self):
        for case in _load_fixture_cases():
            assert case.commit_message.strip()
            lo, hi = case.expected_signal_range
            assert 0.0 <= lo <= hi <= 1.0

    def test_fixture_case_names_match_inline(self):
        fixture_names = {c.name for c in _load_fixture_cases()}
        # The fixture is a subset of the inline cases
        inline_names = {c.name for c in EXTRACTION_EVAL_CASES}
        assert fixture_names.issubset(inline_names)


# ---------------------------------------------------------------------------
# Protocol compliance test
# ---------------------------------------------------------------------------


def test_anthropic_provider_implements_protocol():
    """AnthropicProvider must satisfy the ExtractionProvider Protocol."""
    from nauro.extraction.anthropic_provider import AnthropicProvider
    from nauro.extraction.providers import ExtractionProvider

    provider = AnthropicProvider(api_key="test-key")
    assert isinstance(provider, ExtractionProvider)


# ---------------------------------------------------------------------------
# Unit tests: extract_from_commit (mocked API)
# ---------------------------------------------------------------------------


def _make_mock_response(tool_input: dict) -> MagicMock:
    """Build a mock Anthropic API response with a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_extraction"
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    return response


def _make_new_format_result(
    decisions=None,
    questions=None,
    state_delta=None,
    signal=None,
    composite_score=0.0,
    skip=False,
    reasoning="Test reasoning",
) -> ExtractionResult:
    """Build an ExtractionResult for testing."""
    signal_dict = signal or {
        "architectural_significance": 0.0,
        "novelty": 0.0,
        "rationale_density": 0.0,
        "reversibility": 0.0,
        "scope": 0.0,
    }
    return ExtractionResult(
        decisions=decisions or [],
        questions=questions or [],
        state_delta=state_delta,
        signal=SignalScore(
            architectural_significance=signal_dict.get("architectural_significance", 0.0),
            novelty=signal_dict.get("novelty", 0.0),
            rationale_density=signal_dict.get("rationale_density", 0.0),
            reversibility=signal_dict.get("reversibility", 0.0),
            scope=signal_dict.get("scope", 0.0),
            composite_score=composite_score,
            reasoning=reasoning,
        ),
        skip=skip,
        reasoning=reasoning,
    )


class TestExtractFromCommit:
    """Test extract_from_commit with mocked Anthropic client."""

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_returns_parsed_tool_result(self, mock_cls):
        expected = _make_new_format_result(
            decisions=[{"title": "Switch to Postgres", "confidence": "high"}],
            signal={
                "architectural_significance": 0.9,
                "novelty": 0.7,
                "rationale_density": 0.5,
                "reversibility": 0.8,
                "scope": 0.6,
            },
            composite_score=0.75,
        )
        mock_cls.return_value.messages.create.return_value = _make_mock_response(expected.to_dict())

        result = extract_from_commit("migrate db", "+ postgres", ["tasks.py"], api_key="test")
        assert isinstance(result, ExtractionResult)
        assert result.signal.composite_score == 0.75
        assert result.skip is False
        assert len(result.decisions) == 1
        assert result.signal.architectural_significance == 0.9
        assert result.reasoning == "Test reasoning"

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_returns_skip_on_api_error(self, mock_cls):
        mock_cls.return_value.messages.create.side_effect = Exception("API down")

        result = extract_from_commit("test", "diff", ["f.py"], api_key="test")
        assert isinstance(result, ExtractionSkipped)
        assert result.reason == "error"

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_returns_skip_when_no_tool_use_block(self, mock_cls):
        text_block = MagicMock()
        text_block.type = "text"
        response = MagicMock()
        response.content = [text_block]
        mock_cls.return_value.messages.create.return_value = response

        result = extract_from_commit("test", "diff", ["f.py"], api_key="test")
        assert isinstance(result, ExtractionSkipped)
        assert result.reason == "no_tool_use"

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_defaults_missing_keys(self, mock_cls):
        mock_cls.return_value.messages.create.return_value = _make_mock_response(
            {"composite_score": 0.5, "skip": False}
        )

        result = extract_from_commit("test", "diff", ["f.py"], api_key="test")
        assert isinstance(result, ExtractionResult)
        assert result.decisions == []
        assert result.questions == []
        assert result.state_delta is None
        assert result.signal is not None
        assert result.reasoning == ""

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_recomputes_composite_when_zero_with_decisions(self, mock_cls):
        """If model returns skip=false, real decisions, but composite_score=0.0,
        the pipeline should recompute from signal dimensions."""
        mock_cls.return_value.messages.create.return_value = _make_mock_response(
            {
                "decisions": [{"title": "Use event-driven sync", "confidence": "high"}],
                "questions": [],
                "state_delta": "Replaced daemon with hooks",
                "signal": {
                    "architectural_significance": 0.8,
                    "novelty": 0.6,
                    "rationale_density": 0.7,
                    "reversibility": 0.5,
                    "scope": 0.7,
                },
                "composite_score": 0.0,
                "skip": False,
                "reasoning": "Major architecture change",
            }
        )

        result = extract_from_commit("add hooks", "diff", ["hooks.py"], api_key="test")
        assert isinstance(result, ExtractionResult)
        assert result.signal.composite_score > 0.0
        # Expected: 0.8*0.3 + 0.6*0.2 + 0.7*0.2 + 0.5*0.15 + 0.7*0.15 = 0.68
        assert result.signal.composite_score == pytest.approx(0.68)
        assert result.skip is False

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_no_recompute_when_skip_true(self, mock_cls):
        """Don't recompute if skip=true — 0.0 is correct for skipped results."""
        mock_cls.return_value.messages.create.return_value = _make_mock_response(
            {
                "decisions": [],
                "questions": [],
                "state_delta": None,
                "signal": {
                    "architectural_significance": 0.0,
                    "novelty": 0.0,
                    "rationale_density": 0.0,
                    "reversibility": 0.0,
                    "scope": 0.0,
                },
                "composite_score": 0.0,
                "skip": True,
                "reasoning": "Trivial",
            }
        )

        result = extract_from_commit("typo", "diff", ["f.py"], api_key="test")
        assert isinstance(result, ExtractionResult)
        assert result.signal.composite_score == 0.0

    @patch("nauro.extraction.anthropic_provider.anthropic.Anthropic")
    def test_no_recompute_when_composite_nonzero(self, mock_cls):
        """Don't recompute if model already provided a nonzero composite."""
        mock_cls.return_value.messages.create.return_value = _make_mock_response(
            {
                "decisions": [{"title": "Switch DB", "confidence": "high"}],
                "questions": [],
                "state_delta": None,
                "signal": {
                    "architectural_significance": 0.9,
                    "novelty": 0.7,
                    "rationale_density": 0.5,
                    "reversibility": 0.8,
                    "scope": 0.6,
                },
                "composite_score": 0.75,
                "skip": False,
                "reasoning": "Major change",
            }
        )

        result = extract_from_commit("switch db", "diff", ["db.py"], api_key="test")
        assert isinstance(result, ExtractionResult)
        assert result.signal.composite_score == 0.75  # Model's value, not recomputed


# ---------------------------------------------------------------------------
# Unit tests: process_commit (mocked extraction + real store)
# ---------------------------------------------------------------------------


class TestProcessCommit:
    """Test that process_commit correctly routes content to store files."""

    def _scaffold(self, tmp_path: Path) -> Path:
        store = tmp_path / "store"
        scaffold_project_store("test-project", store)
        return store

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_writes_decisions_to_store(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("add feature", "1 file changed", ["src/f.py"])
        mock_extract.return_value = _make_new_format_result(
            decisions=[
                {
                    "title": "Use Postgres",
                    "rationale": (
                        "Better fit for JSON operations and mature ecosystem"
                        " for production workloads"
                    ),
                    "confidence": "high",
                    "decision_type": "architecture",
                    "reversibility": "hard",
                    "files_affected": ["src/db.py"],
                }
            ],
            signal={
                "architectural_significance": 0.9,
                "novelty": 0.7,
                "rationale_density": 0.6,
                "reversibility": 0.8,
                "scope": 0.5,
            },
            composite_score=0.75,
        )

        result = process_commit("/fake/repo", store, threshold=0.4)
        assert result is not None
        decisions = list((store / "decisions").glob("*.md"))
        assert len(decisions) == 2  # 001-initial-setup + 002-use-postgres
        user_decisions = [d for d in decisions if not d.name.startswith("001-initial")]
        assert len(user_decisions) == 1
        content = user_decisions[0].read_text()
        assert "Use Postgres" in content
        assert "decision_type: architecture" in content
        assert "reversibility: hard" in content

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_appends_questions_to_store(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("wip", "1 file changed", ["src/f.py"])
        mock_extract.return_value = _make_new_format_result(
            questions=["Should we use soft delete?"],
            signal={
                "architectural_significance": 0.5,
                "novelty": 0.4,
                "rationale_density": 0.6,
                "reversibility": 0.3,
                "scope": 0.3,
            },
            composite_score=0.45,
        )

        process_commit("/fake/repo", store, threshold=0.4)
        oq = (store / "open-questions.md").read_text()
        assert "soft delete" in oq

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_updates_state_delta(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("refactor", "2 files changed", ["a.py", "b.py"])
        mock_extract.return_value = _make_new_format_result(
            state_delta="Split store into reader/writer",
            signal={
                "architectural_significance": 0.4,
                "novelty": 0.3,
                "rationale_density": 0.4,
                "reversibility": 0.2,
                "scope": 0.5,
            },
            composite_score=0.37,
            reasoning="Module reorganization",
        )

        # threshold=0.3 to ensure this gets captured
        process_commit("/fake/repo", store, threshold=0.3)
        state = (store / "state_current.md").read_text()
        assert "Split store into reader/writer" in state

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_skips_below_threshold(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("fix typo", "1 file changed", ["README.md"])
        mock_extract.return_value = _make_new_format_result(
            composite_score=0.1,
            reasoning="Trivial typo fix",
        )

        result = process_commit("/fake/repo", store, threshold=0.4)
        assert result is None
        decisions = list((store / "decisions").glob("*.md"))
        assert len(decisions) == 1
        assert decisions[0].name == "001-initial-setup.md"

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_skips_when_skip_true(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("merge", "", [])
        mock_extract.return_value = _make_new_format_result(
            skip=True,
            composite_score=0.05,
            reasoning="Merge commit, no unique content",
        )

        result = process_commit("/fake/repo", store, threshold=0.4)
        assert result is None

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_captures_snapshot_on_write(self, mock_extract, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("add feature", "1 file changed", ["src/f.py"])
        mock_extract.return_value = _make_new_format_result(
            decisions=[
                {
                    "title": "New API",
                    "rationale": "Need a REST endpoint for the new client integration",
                    "confidence": "medium",
                }
            ],
            state_delta="Added new API endpoint",
            signal={
                "architectural_significance": 0.7,
                "novelty": 0.5,
                "rationale_density": 0.4,
                "reversibility": 0.3,
                "scope": 0.5,
            },
            composite_score=0.55,
        )

        process_commit("/fake/repo", store, threshold=0.4)
        snapshots = list((store / "snapshots").glob("v*.json"))
        assert len(snapshots) >= 1

    @patch("nauro.extraction.pipeline.get_commit_info")
    def test_graceful_on_empty_commit(self, mock_git, tmp_path):
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("", "", [])

        result = process_commit("/fake/repo", store, threshold=0.4)
        assert result is None

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_respects_env_threshold(self, mock_extract, mock_git, tmp_path, monkeypatch):
        store = self._scaffold(tmp_path)
        monkeypatch.setenv("NAURO_SIGNAL_THRESHOLD", "0.9")
        mock_git.return_value = ("refactor", "1 file", ["a.py"])
        mock_extract.return_value = _make_new_format_result(
            state_delta="Refactored",
            composite_score=0.6,
            reasoning="Medium refactor",
        )

        result = process_commit("/fake/repo", store, threshold=None)
        assert result is None  # 0.6 < 0.9 threshold

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_extraction_log_written(self, mock_extract, mock_git, tmp_path):
        """Extraction-log.jsonl is written on every extraction attempt."""
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("feature", "1 file", ["a.py"])
        mock_extract.return_value = _make_new_format_result(
            composite_score=0.7,
            reasoning="Test log entry",
        )

        process_commit("/fake/repo", store, threshold=0.4)
        log_path = store / "extraction-log.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert "timestamp" in entry
        assert entry["source"] == "commit"
        assert entry["reasoning"] == "Test log entry"

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_extraction_log_written_on_skip(self, mock_extract, mock_git, tmp_path):
        """Extraction-log.jsonl is written even when extraction is skipped."""
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("typo fix", "1 file", ["README.md"])
        mock_extract.return_value = _make_new_format_result(
            skip=True,
            composite_score=0.0,
            reasoning="Trivial change",
        )

        process_commit("/fake/repo", store, threshold=0.4)
        log_path = store / "extraction-log.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["captured"] is False

    @patch("nauro.extraction.pipeline.get_commit_info")
    @patch("nauro.extraction.pipeline.extract_from_commit")
    def test_handles_rejected_as_objects(self, mock_extract, mock_git, tmp_path):
        """New-format rejected alternatives (objects) are written correctly."""
        store = self._scaffold(tmp_path)
        mock_git.return_value = ("switch db", "1 file", ["db.py"])
        mock_extract.return_value = _make_new_format_result(
            decisions=[
                {
                    "title": "Use Postgres over SQLite",
                    "rationale": "Need concurrent writes for multi-user access patterns",
                    "confidence": "high",
                    "rejected": [
                        {"alternative": "SQLite", "reason": "No concurrent write support"},
                        {"alternative": "MySQL", "reason": "Weaker JSON support"},
                    ],
                    "decision_type": "data_model",
                    "reversibility": "hard",
                    "files_affected": ["src/db.py", "migrations/"],
                }
            ],
            composite_score=0.8,
        )

        process_commit("/fake/repo", store, threshold=0.4)
        decisions = [d for d in (store / "decisions").glob("*.md") if "initial" not in d.name]
        assert len(decisions) == 1
        content = decisions[0].read_text()
        assert "### SQLite" in content
        assert "No concurrent write support" in content
        assert "### MySQL" in content
        assert "decision_type: data_model" in content


# ---------------------------------------------------------------------------
# Integration tests — hit the real API (skipped in CI)
# ---------------------------------------------------------------------------


@pytest.fixture(params=EXTRACTION_EVAL_CASES, ids=lambda c: c.name)
def eval_case(request) -> ExtractionTestCase:
    """Parametrized fixture yielding each eval case."""
    return request.param


def test_eval_case_is_well_formed(eval_case: ExtractionTestCase):
    """Each eval case can produce a valid user prompt."""
    prompt = build_extraction_user_prompt(
        eval_case.commit_message,
        eval_case.diff_summary,
        eval_case.changed_files,
    )
    assert len(prompt) > 0
    assert eval_case.commit_message in prompt


@pytest.mark.integration
def test_extraction_pipeline(eval_case: ExtractionTestCase):
    """Run each eval case through the real LLM pipeline.

    Requires ANTHROPIC_API_KEY to be set. Skip in CI with:
        pytest -m "not integration"
    """
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    outcome = extract_from_commit(
        eval_case.commit_message,
        eval_case.diff_summary,
        eval_case.changed_files,
    )

    # Must return ExtractionResult (not ExtractionSkipped) for integration tests
    assert isinstance(outcome, ExtractionResult), (
        f"{eval_case.name}: expected ExtractionResult, got {type(outcome)}"
    )
    result = outcome

    # Validate skip expectation
    if eval_case.expected_skip:
        assert result.skip is True, f"{eval_case.name}: expected skip=True, got {result.skip}"

    # Validate signal range (using composite_score)
    lo, hi = eval_case.expected_signal_range
    assert lo <= result.signal.composite_score <= hi, (
        f"{eval_case.name}: composite_score {result.signal.composite_score} not in ({lo}, {hi})"
    )

    # Validate decisions
    if eval_case.expected_has_decisions:
        assert len(result.decisions) > 0, f"{eval_case.name}: expected decisions but got none"

    # Validate questions
    if eval_case.expected_has_questions:
        assert len(result.questions) > 0, f"{eval_case.name}: expected questions but got none"

    # Validate state delta
    if eval_case.expected_has_state_delta is True:
        assert result.state_delta is not None, (
            f"{eval_case.name}: expected state_delta but got None"
        )
    elif eval_case.expected_has_state_delta is False:
        assert result.state_delta is None

    # If skip=True, verify empty outputs per prompt rules
    if result.skip:
        assert result.decisions == []
        assert result.questions == []
        assert result.state_delta is None

    # Reasoning should always be populated
    assert isinstance(result.reasoning, str)


# ---------------------------------------------------------------------------
# Prompt quality rule tests
# ---------------------------------------------------------------------------


class TestPromptQualityRules:
    """Verify the extraction prompt contains the quality rules that prevent
    known failure modes (D25-29 over-splitting, circular rationale, etc.)."""

    def test_consolidation_rule_present(self):
        """Prompt must instruct 0-2 decisions per commit to prevent over-splitting."""
        assert "0-2 decisions" in EXTRACTION_SYSTEM_PROMPT
        assert "never more than 3" in EXTRACTION_SYSTEM_PROMPT
        assert "over-splitting" in EXTRACTION_SYSTEM_PROMPT

    def test_rationale_quality_gate_present(self):
        """Prompt must distinguish WHAT (description) from WHY (rationale)."""
        assert "WHAT was implemented is not rationale" in EXTRACTION_SYSTEM_PROMPT

    def test_anti_inference_rule_present(self):
        """Prompt must warn against inferring decisions from file names."""
        assert "Do not infer decisions from file structure" in EXTRACTION_SYSTEM_PROMPT

    def test_questions_filter_present(self):
        """Prompt must prevent questions about things the commit resolves."""
        assert (
            "Do NOT generate questions about things that the commit itself"
            in EXTRACTION_SYSTEM_PROMPT
        )

    def test_rationale_example_present(self):
        """Prompt should include the concrete S3/R2 example."""
        assert "S3 was chosen over R2" in EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Dedup-aware user prompt tests
# ---------------------------------------------------------------------------


class TestDedupUserPrompt:
    """Verify that build_extraction_user_prompt handles existing_decisions."""

    def test_no_dedup_section_when_none(self):
        result = build_extraction_user_prompt("msg", "diff", ["f.py"])
        assert "Existing decisions" not in result

    def test_no_dedup_section_when_empty(self):
        result = build_extraction_user_prompt("msg", "diff", ["f.py"], existing_decisions=[])
        assert "Existing decisions" not in result

    def test_dedup_section_included(self):
        titles = ["Use S3 for sync", "Replace daemon with event-driven sync"]
        result = build_extraction_user_prompt("msg", "diff", ["f.py"], existing_decisions=titles)
        assert "## Existing decisions in store" in result
        assert "Use S3 for sync" in result
        assert "Replace daemon with event-driven sync" in result
        assert "Do not extract decisions that substantially overlap" in result

    def test_dedup_section_formats_as_list(self):
        titles = ["Decision A", "Decision B"]
        result = build_extraction_user_prompt("msg", "diff", ["f.py"], existing_decisions=titles)
        assert "  - Decision A" in result
        assert "  - Decision B" in result


# ---------------------------------------------------------------------------
# Over-splitting eval cases — modeled on real D25-29 and D45-49 failures
# ---------------------------------------------------------------------------


OVER_SPLIT_EVAL_CASES: list[ExtractionTestCase] = [
    ExtractionTestCase(
        name="sync_layer_multi_module",
        commit_message="Add cloud sync layer: S3-backed multi-machine sync for project stores",
        diff_summary="""\
+ src/nauro/sync/__init__.py
+ src/nauro/sync/config.py — SyncConfig dataclass, load from ~/.nauro/config.json
+ src/nauro/sync/daemon.py — background sync process with 30s polling
+ src/nauro/sync/merge.py — union merge for decisions, last-write-wins for state
+ src/nauro/sync/remote.py — S3 client wrapper, list/pull/push operations
+ src/nauro/sync/state.py — SHA256 tracking, ETag comparison, .sync-state.json
+ src/nauro/cli/commands/sync.py — nauro sync CLI command
+ tests/test_sync/ — 12 test files""",
        changed_files=[
            "src/nauro/sync/__init__.py",
            "src/nauro/sync/config.py",
            "src/nauro/sync/daemon.py",
            "src/nauro/sync/merge.py",
            "src/nauro/sync/remote.py",
            "src/nauro/sync/state.py",
            "src/nauro/cli/commands/sync.py",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 0.9),
        expected_has_decisions=True,
        expected_has_state_delta=True,
    ),
    ExtractionTestCase(
        name="bidirectional_sync_batch",
        commit_message=(
            "Add bidirectional sync, graceful API key degradation,"
            " event-driven sync hooks, nauro status"
        ),
        diff_summary="""\
+ src/nauro/sync/hooks.py — push_after_extraction, pull_before_session
- polling loop in daemon.py replaced with hook triggers
+ src/nauro/cli/commands/status.py — nauro status command
+ src/nauro/extraction/pipeline.py — _has_api_key, _make_no_api_key_result
+ tests/test_sync/test_hooks.py
+ tests/test_cli_status.py
+ tests/test_extraction_api_key/
22 files changed, 914 insertions(+), 328 deletions(-)""",
        changed_files=[
            "src/nauro/sync/hooks.py",
            "src/nauro/sync/daemon.py",
            "src/nauro/cli/commands/status.py",
            "src/nauro/cli/commands/sync.py",
            "src/nauro/extraction/pipeline.py",
            "src/nauro/extraction/session_extractor.py",
        ],
        expected_skip=False,
        expected_signal_range=(0.5, 0.9),
        expected_has_decisions=True,
        expected_has_state_delta=True,
    ),
]


class TestOverSplitCaseFormat:
    """Validate over-splitting eval cases are well-formed."""

    def test_cases_exist(self):
        assert len(OVER_SPLIT_EVAL_CASES) >= 2

    def test_cases_well_formed(self):
        for case in OVER_SPLIT_EVAL_CASES:
            assert case.commit_message.strip()
            lo, hi = case.expected_signal_range
            assert 0.0 <= lo <= hi <= 1.0


@pytest.mark.integration
@pytest.mark.parametrize(
    "eval_case",
    OVER_SPLIT_EVAL_CASES,
    ids=lambda c: c.name,
)
def test_over_split_max_decisions(eval_case: ExtractionTestCase):
    """A single commit should produce at most 2 decisions.

    This test catches the D25-29 failure mode where one commit generated 5
    thin decisions (one per module file).
    """
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    outcome = extract_from_commit(
        eval_case.commit_message,
        eval_case.diff_summary,
        eval_case.changed_files,
    )

    assert isinstance(outcome, ExtractionResult)
    assert len(outcome.decisions) <= 3, (
        f"{eval_case.name}: extracted {len(outcome.decisions)} decisions "
        f"(max 3). Titles: {[d.get('title') for d in outcome.decisions]}"
    )
