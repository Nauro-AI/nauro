from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from nauro.mcp import generation_responses as generation
from nauro.mcp import read_dispatch as dispatch
from nauro.mcp import stdio_server
from nauro.store import read_authority
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.store.resolution import resolve_project_binding
from nauro.sync.generation_session import GenerationTransferSession
from tests.generation_account import seed_generation_account
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import POSIX
from tests.test_generation_reads import admitted as admitted

CASES = [("get_context", {"level": level}) for level in ("L0", "L1", "L2")] + [
    ("get_decision", {"number": 1, "mode": "full"}),
    ("get_decision", {"number": 1, "mode": "header"}),
    ("get_raw_file", {"path": "state.md"}),
    ("get_raw_file", {"path": "project.md"}),
    ("list_decisions", {"limit": 1, "include_superseded": True}),
    ("search_decisions", {"query": "durability", "limit": 1}),
    ("check_decision", {"proposed_approach": "durability", "context": "refresh"}),
]
ERROR = CallToolResult(
    content=[TextContent(type="text", text="Error: Project read authority is unavailable.")],
    isError=True,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "account-home"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))


@pytest.fixture
def cloud(admitted, monkeypatch):
    binding, current, checks = admitted
    register_project_v2(
        binding.display_name,
        [],
        mode="cloud",
        project_id=binding.project_id,
        server_url=binding.server_url,
    )
    seed_generation_account(binding, USER_ID, monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        monkeypatch.setattr(
            dispatch, "GenerationTransferSession", lambda b: GenerationTransferSession(b, client)
        )
        yield binding, current, checks


@pytest.fixture
def flat():
    pid, path = register_project_v2("Flat", [])
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.md").write_text("Flat state")
    (path / "project.md").write_text("# Flat Project")
    return resolve_project_binding(pid, None)


def forbidden(*args, **kwargs):
    pytest.fail("Wrong authority path executed")


@POSIX
@pytest.mark.parametrize("name,kwargs", CASES)
def test_generation_uses_prepared_text_without_legacy_render(cloud, monkeypatch, name, kwargs):
    binding, _, checks = cloud
    assert resolve_project_binding(binding.project_id, None) == binding
    assert read_authority.observe_generation_marker(binding) is not None
    with GenerationTransferSession(binding) as session:
        assert session.credentials().user_id == USER_ID
    expected = getattr(generation, name)(binding, **kwargs, actor=USER_ID)
    assert expected.is_error is False
    monkeypatch.setattr(dispatch, "_legacy_result", forbidden)
    monkeypatch.setattr(dispatch.legacy, f"tool_{name}", forbidden)
    checks.clear()
    actual = getattr(dispatch, name)(project_id=binding.project_id, **kwargs)
    assert actual == CallToolResult(
        content=[TextContent(type="text", text=expected.text)], isError=False
    )
    assert actual.structuredContent is None
    assert len(checks) == 5


@pytest.mark.parametrize("name,kwargs", CASES + [("diff_since_last_session", {})])
def test_flat_matches_legacy_adapter_without_credentials(flat, monkeypatch, name, kwargs):
    monkeypatch.setattr(dispatch, "GenerationTransferSession", forbidden)
    envelope = getattr(dispatch.legacy, f"tool_{name}")(flat.store_path, **kwargs)
    expected = stdio_server._wrap_with_renderer(
        name, envelope, dispatch.resolve_renderer_kwargs(name, kwargs, flat.store_path)
    )
    actual = getattr(dispatch, name)(project_id=flat.project_id, **kwargs)
    assert actual == expected
    assert getattr(stdio_server, name)(project_id=flat.project_id, **kwargs) == expected


@pytest.mark.parametrize("phase", ["handler", "renderer"])
def test_flat_cutover_discards_response(flat, monkeypatch, phase):
    def cutover():
        root = flat.store_path / ".replica"
        root.mkdir()
        (root / "authority.json").write_text("CORRUPT NEW AUTHORITY")

    if phase == "handler":
        original = dispatch.legacy.tool_get_raw_file

        def changed(*a, **k):
            result = original(*a, **k)
            cutover()
            return result

        monkeypatch.setattr(dispatch.legacy, "tool_get_raw_file", changed)
    else:
        original = dispatch.try_render_envelope

        def changed(*a, **k):
            result = original(*a, **k)
            cutover()
            return result

        monkeypatch.setattr(dispatch, "try_render_envelope", changed)
    assert dispatch.get_raw_file("state.md", project_id=flat.project_id) == ERROR


@POSIX
@pytest.mark.parametrize(
    "defect",
    ["corrupt", "missing_intent", "credentials", "account_change", "marker_change", "renderer"],
)
def test_generation_failures_never_fall_back(cloud, monkeypatch, defect):
    binding, _, _ = cloud
    marker = binding.store_path / ".replica/authority.json"
    if defect == "corrupt":
        marker.write_text("PRIVATE")
    elif defect == "missing_intent":
        (binding.store_path / f".replica/v1/actors/{USER_ID}/refresh-intent.json").unlink()
    elif defect == "credentials":
        save_config({})
    else:
        original = generation.get_raw_file

        def changed(*a, **k):
            result = original(*a, **k)
            if defect == "account_change":
                save_config(
                    {"auth": {"user_id": "01K44444444444444444444444", "access_token": "changed"}}
                )
            elif defect == "marker_change":
                marker.unlink()
            return result

        monkeypatch.setattr(generation, "get_raw_file", changed)
        if defect == "renderer":
            from nauro_core.renderers import RENDERERS

            def fail(*a, **k):
                raise RuntimeError("PRIVATE CONTENT")

            monkeypatch.setitem(RENDERERS, "get_raw_file", fail)
    monkeypatch.setattr(dispatch.legacy, "tool_get_raw_file", forbidden)
    result = dispatch.get_raw_file("state.md", project_id=binding.project_id)
    assert result.isError is True
    assert result.structuredContent is None
    assert len(result.content) == 1
    assert "Verified generation state" not in str(result)
    assert "PRIVATE" not in str(result)


@POSIX
def test_generation_history_never_reads_flat_snapshots(cloud, monkeypatch):
    binding, _, _ = cloud
    monkeypatch.setattr(dispatch.legacy, "tool_diff_since_last_session", forbidden)
    result = dispatch.diff_since_last_session(project_id=binding.project_id)
    assert "Generation read unavailable" in result.content[0].text
    assert result.isError is True


@pytest.mark.parametrize(
    "defect",
    [
        "directory",
        pytest.param("symlink", marks=POSIX),
        "hardlink",
        "large",
        "local_marker",
        "noncanonical",
        "wrong_project",
        "unsupported",
        "denied",
    ],
)
def test_marker_observation_refuses_unsafe_evidence(flat, monkeypatch, defect):
    root = flat.store_path / ".replica"
    root.mkdir()
    path = root / "authority.json"
    marker = {
        "schema_version": 1,
        "authority": "generation",
        "project_id": flat.project_id,
        "store_format_version": 1,
    }
    if defect == "directory":
        path.mkdir()
    elif defect in ("symlink", "hardlink"):
        target = flat.store_path / "target"
        target.write_text("{}")
        if defect == "symlink":
            path.symlink_to(target)
        else:
            path.hardlink_to(target)
    elif defect == "large":
        path.write_bytes(b"x" * 20000)
    elif defect == "denied":

        def denied(*a, **k):
            raise PermissionError("PRIVATE")

        monkeypatch.setattr(read_authority, "_read_optional_file", denied)
    else:
        if defect == "wrong_project":
            marker["project_id"] = "01K33333333333333333333333"
        elif defect == "unsupported":
            marker["store_format_version"] = 2
        path.write_text(
            json.dumps(
                marker, sort_keys=True, separators=None if defect == "noncanonical" else (",", ":")
            )
        )
    monkeypatch.setattr(dispatch.legacy, "tool_get_raw_file", forbidden)
    assert dispatch.get_raw_file("state.md", project_id=flat.project_id) == ERROR


def test_resolution_failure_never_calls_legacy(monkeypatch):
    monkeypatch.setattr(dispatch.legacy, "tool_get_context", forbidden)
    assert dispatch.get_context(project_id="missing") == ERROR


def test_dispatch_has_only_named_consumers_and_keeps_public_arguments():
    root = Path(dispatch.__file__).parents[1]
    for path in root.rglob("*.py"):
        if path.relative_to(root).as_posix() in {"cli/generation_reads.py", "mcp/stdio_server.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(a.name != "nauro.mcp.read_dispatch" for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "nauro.mcp.read_dispatch"
                if node.module == "nauro.mcp":
                    assert all(a.name != "read_dispatch" for a in node.names)
    for name, _ in CASES + [("diff_since_last_session", {})]:
        live = inspect.signature(getattr(stdio_server, name)).parameters
        new = inspect.signature(getattr(dispatch, name)).parameters
        assert list(new) == list(live)
        assert [p.default for p in new.values()] == [p.default for p in live.values()]


@POSIX
@pytest.mark.parametrize("defect", ["none", "noncanonical", "wrong_project", "unsupported"])
def test_cloud_marker_validation_before_generation_admission(cloud, monkeypatch, defect):
    from nauro.store.generation_authority import (
        ClientUpgradeRequiredError,
        GenerationControlCorruptError,
    )

    binding, _, _ = cloud
    path = binding.store_path / ".replica/authority.json"
    original = path.read_bytes()
    marker = json.loads(original)
    if defect == "none":
        assert read_authority.observe_generation_marker(binding) == original
        return
    if defect == "wrong_project":
        marker["project_id"] = "01K44444444444444444444444"
    elif defect == "unsupported":
        marker["store_format_version"] = 2
    path.write_text(
        json.dumps(
            marker, sort_keys=True, separators=None if defect == "noncanonical" else (",", ":")
        )
    )
    error = ClientUpgradeRequiredError if defect == "unsupported" else GenerationControlCorruptError
    with pytest.raises(error):
        read_authority.observe_generation_marker(binding)
    monkeypatch.setattr(generation, "get_context", forbidden)
    assert dispatch.get_context(project_id=binding.project_id) == ERROR


@POSIX
@pytest.mark.parametrize(
    "path",
    [
        "questions-provenance.json",
        "./questions-provenance.json",
        "../state.md",
        ".replica/authority.json",
    ],
)
def test_generation_raw_exclusions_survive_dispatch(cloud, path):
    binding, _, checks = cloud
    checks.clear()
    result = dispatch.get_raw_file(path, project_id=binding.project_id)
    assert result == dispatch._prepared(
        generation._error("Invalid or unavailable generation path.", kind="rejected")
    )
    assert checks == []


@pytest.mark.parametrize("phase", ["handler", "renderer"])
def test_valid_generation_cutover_discards_legacy_bytes(flat, monkeypatch, phase):
    from nauro.store.resolution import ResolvedProjectBinding

    binding = ResolvedProjectBinding(
        flat.store_path, flat.project_id, flat.display_name, "cloud", "https://example.test"
    )
    monkeypatch.setattr(dispatch, "resolve_project_binding", lambda *a: binding)
    marker = {
        "schema_version": 1,
        "authority": "generation",
        "project_id": binding.project_id,
        "store_format_version": 1,
    }

    def cutover():
        root = binding.store_path / ".replica"
        root.mkdir()
        (root / "authority.json").write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":"))
        )

    owner = dispatch.legacy if phase == "handler" else dispatch
    name = "tool_get_raw_file" if phase == "handler" else "try_render_envelope"
    original = getattr(owner, name)

    def changed(*a, **k):
        result = original(*a, **k)
        cutover()
        return result

    monkeypatch.setattr(owner, name, changed)
    assert dispatch.get_raw_file("state.md", project_id=binding.project_id) == ERROR
