from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from nauro_core import renderers
from nauro_core.renderers import L2_CHAR_BUDGET

from nauro.mcp import generation_responses as responses
from nauro.store import generation_installation as installation
from nauro.store.generation_authority import RefreshRequiredError
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID, _projection
from tests.test_generation_reads import CASES, POSIX
from tests.test_generation_reads import admitted as admitted
from tests.test_generation_refresh import _target


@POSIX
@pytest.mark.parametrize("name,args,kwargs", CASES)
def test_response_content_and_metadata_use_one_generation(admitted, name, args, kwargs):
    binding, current, checks = admitted
    (binding.store_path / "state.md").write_text("**Last synced:** POISON")
    (binding.store_path / "project.md").write_text("POISON")
    snapshots = binding.store_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "001.json").write_text("POISON")
    binding = replace(binding, display_name="POISON REGISTRY LABEL")
    # The authenticated response uses this operation's validated binding.
    current[0] = replace_projection_binding(current[0], binding)
    for _ in range(2):
        result = getattr(responses, name)(binding, *args, actor=USER_ID, **kwargs)
        identity = current[0].target.identity
        assert result.is_error is False
        assert result.envelope["project"] == {"id": binding.project_id, "name": binding.project_id}
        assert result.envelope["read_authority"] == {
            "kind": "generation",
            "project_id": identity.project_id,
            "generation_id": identity.generation_id,
            "manifest_digest": identity.manifest_digest,
            "committed_at": identity.committed_at,
            "freshness": "authorized_at_read",
        }
        assert result.text.endswith(
            f"Generation: {identity.generation_id}. Committed: {identity.committed_at}.\n"
            "Authorization checked for this read."
        )
        assert "POISON" not in repr(result)
        assert USER_ID not in repr(result)
        assert identity.projection_scope_id not in repr(result)
        assert len(checks) == 5
        checks.clear()


def replace_projection_binding(projection, binding):
    from nauro.store.generation_projection import (
        GenerationProjectionTarget,
        verify_generation_projection,
    )

    return verify_generation_projection(
        GenerationProjectionTarget(binding, projection.target.identity),
        manifest_json=projection.manifest_json,
        artifacts=tuple((a.path, a.content) for a in projection.artifacts),
    )


@POSIX
@pytest.mark.parametrize("name,args,kwargs", CASES)
@pytest.mark.parametrize("failure", ["scope", "account", "renderer"])
def test_no_payload_escapes_a_failure_during_rendering(
    admitted, monkeypatch, name, args, kwargs, failure
):
    binding, current, _ = admitted
    original = renderers.RENDERERS[name]

    def interrupt(*a, **k):
        if failure == "scope":
            current[0] = _target()
        elif failure == "account":
            monkeypatch.setattr(
                installation, "read_active_user_id", lambda: "01K44444444444444444444444"
            )
        else:
            raise RuntimeError("PRIVATE CAPTURED CONTENT")
        return original(*a, **k)

    monkeypatch.setitem(renderers.RENDERERS, name, interrupt)
    result = getattr(responses, name)(binding, *args, actor=USER_ID, **kwargs)
    assert result.is_error is True
    assert set(result.envelope) == {"store", "error"}
    assert "Verified generation state" not in repr(result)
    assert "Require durability" not in repr(result)
    assert "PRIVATE" not in repr(result)
    assert len(result.text) < 150


@POSIX
@pytest.mark.parametrize("path", ["./state.md", "state.md", ".//state.md"])
def test_raw_path_normalization_is_lexical(admitted, path):
    binding, _, _ = admitted
    result = responses.get_raw_file(binding, path, actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["content"] == "# State\n\nVerified generation state.\n"


@POSIX
@pytest.mark.parametrize(
    "path",
    [
        "../state.md",
        "context/../state.md",
        "/state.md",
        "context\\brief.md",
        ".replica/authority.json",
        "questions-provenance.json",
        "./questions-provenance.json",
        "",
        "snapshots/001.json",
    ],
)
def test_raw_exclusions_refuse_before_read(admitted, monkeypatch, path):
    binding, _, checks = admitted

    def forbidden(*a, **k):
        pytest.fail("Invalid generic path reached generation admission.")

    monkeypatch.setattr(responses.reads, "get_raw_file", forbidden)
    result = responses.get_raw_file(binding, path, actor=USER_ID)
    assert result.envelope == {
        "store": "local",
        "error": {"kind": "rejected", "reason": "Invalid or unavailable generation path."},
    }
    assert result.is_error is True
    assert checks == []


@POSIX
def test_missing_intent_never_becomes_onboarding(admitted):
    binding, _, _ = admitted
    refresh.refresh_paths(binding, USER_ID).intent.unlink()
    result = responses.get_context(binding, actor=USER_ID)
    assert result.is_error is True
    assert set(result.envelope) == {"store", "error"}
    assert "welcome" not in result.text.lower()
    assert "read_authority" not in result.envelope


@POSIX
def test_missing_file_has_no_flat_store_hints(admitted):
    binding, _, _ = admitted
    (binding.store_path / "stack.md").write_text("SECRET FLAT STACK")
    result = responses.get_raw_file(binding, "stack.md", actor=USER_ID)
    assert result.envelope == {
        "store": "local",
        "error": {"kind": "error", "reason": "File not found: stack.md"},
    }
    assert result.is_error is True


@POSIX
def test_context_limit_keeps_authority_frame(admitted):
    binding, current, _ = admitted
    current[0] = fresh_projection(
        {"project.md": ("# Project\n" + "x" * (L2_CHAR_BUDGET + 100)).encode()}
    )
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_context(binding, "L2", actor=USER_ID)
    assert result.is_error is False
    assert "x" * 100 not in result.text
    assert "Authorization checked for this read." in result.text
    assert len(result.text) < 2000


def test_history_refusal_is_explicit():
    result = responses.diff_since_last_session()
    assert result.is_error is True
    assert result.envelope == {
        "store": "local",
        "error": {
            "kind": "error",
            "reason": (
                "Generation session history is unavailable until authorized history is supported."
            ),
        },
    }


@POSIX
def test_authority_exception_text_is_not_returned(admitted, monkeypatch):
    binding, _, _ = admitted

    def fail(*a, **k):
        raise RefreshRequiredError("PRIVATE PATH OR CREDENTIAL")

    monkeypatch.setattr(responses.reads, "get_context", fail)
    result = responses.get_context(binding, actor=USER_ID)
    assert result.is_error is True
    assert "PRIVATE" not in repr(result)


def test_response_module_has_no_runtime_consumer():
    root = Path(responses.__file__).parents[1]
    consumers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "nauro.mcp.generation_responses"
                or node.module == "nauro.mcp"
                and any(a.name == "generation_responses" for a in node.names)
            ):
                consumers.append(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import):
                consumers.extend(
                    a.name for a in node.names if a.name == "nauro.mcp.generation_responses"
                )
    assert consumers == []


def fresh_projection(artifacts):
    import hashlib
    import json

    from nauro.store.generation_projection import (
        GenerationProjectionIdentity,
        GenerationProjectionTarget,
        verify_generation_projection,
    )

    seed = _projection(artifacts)
    manifest = json.loads(seed.manifest_json)
    manifest["generation_id"] = "01K77777777777777777777777"
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    fields = seed.target.identity.model_dump()
    fields.update(
        generation_id=manifest["generation_id"], manifest_digest=hashlib.sha256(raw).hexdigest()
    )
    return verify_generation_projection(
        GenerationProjectionTarget(seed.target.binding, GenerationProjectionIdentity(**fields)),
        manifest_json=raw,
        artifacts=tuple(artifacts.items()),
    )


@POSIX
def test_empty_authorized_projection_is_not_onboarding(admitted):
    binding, current, _ = admitted
    current[0] = fresh_projection({})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_context(binding, actor=USER_ID)
    assert result.is_error is False
    assert "read_authority" in result.envelope
    assert "guidance" not in result.envelope
    assert "nauro init" not in result.text
    assert "welcome" not in result.text.lower()


@POSIX
def test_raw_content_budget_and_frame_are_separate(admitted):
    from nauro_core.constants import RAW_FILE_CHAR_BUDGET

    binding, current, _ = admitted
    content = "A" * (RAW_FILE_CHAR_BUDGET + 1000)
    current[0] = fresh_projection({"stack.md": content.encode()})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_raw_file(binding, "./stack.md", actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["content"] == content
    assert result.text.count("A") == RAW_FILE_CHAR_BUDGET + 1
    assert result.text.endswith("Authorization checked for this read.")
    assert len(result.text) < RAW_FILE_CHAR_BUDGET + 1000


@POSIX
@pytest.mark.parametrize("failure", ["marker", "barrier", "network"])
def test_admission_errors_never_release_metadata(admitted, monkeypatch, failure):
    binding, _, _ = admitted
    if failure == "marker":
        refresh.refresh_paths(binding, USER_ID).marker.write_text("CORRUPT")
    elif failure == "barrier":

        def fail(*a, **k):
            raise OSError("PRIVATE disk path")

        monkeypatch.setattr(refresh, "sync_file", fail)
    else:

        def fail(*a, **k):
            raise RefreshRequiredError("PRIVATE network detail")

        monkeypatch.setattr(refresh, "check_generation_projection", fail)
    result = responses.get_context(binding, actor=USER_ID)
    assert result.is_error is True
    assert set(result.envelope) == {"store", "error"}
    assert "PRIVATE" not in repr(result)


@POSIX
def test_missing_renderer_does_not_fall_back_to_json(admitted, monkeypatch):
    binding, _, _ = admitted
    monkeypatch.delitem(renderers.RENDERERS, "get_raw_file")
    result = responses.get_raw_file(binding, "state.md", actor=USER_ID)
    assert result.envelope == {
        "store": "local",
        "error": {"kind": "error", "reason": "Generation response could not be rendered."},
    }
    assert result.is_error is True


@pytest.mark.parametrize("level", ["L3", -1, True])
def test_invalid_context_level_has_bounded_refusal(level):
    result = responses.get_context(_projection().target.binding, level, actor=USER_ID)
    assert result.envelope == {
        "store": "local",
        "error": {"kind": "rejected", "reason": "Invalid level. Use L0, L1 or L2."},
    }
    assert result.is_error is True
