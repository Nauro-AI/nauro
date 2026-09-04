from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import MappingProxyType

import pydantic as pyd
import pytest

from nauro.store import generation_projection
from nauro.store.generation_authority import ClientUpgradeRequiredError
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionManifest,
    GenerationProjectionTarget,
    GenerationProjectionVerificationError,
    VerifiedGenerationArtifact,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
OTHER_PROJECT_ID = "01K00000000000000000000000"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
SCOPE_ID = "a" * 64
COMMITTED_AT = "2999-12-31T23:59:59.999999Z"
CONTENT = b"# Project\n"
BAD_TARGET = "Generation verification requires a validated projection target."
BAD_IDENTITY = "Generation projection targets require a validated projection identity."
BAD_BINDING = "Generation projection targets require a validated cloud binding."
OTHER_PROJECT = "The generation projection belongs to another project."
BAD_FORMAT = "The generation projection uses an unsupported store format."
EXACT_MANIFEST = "The generation manifest must be exact bytes."
BAD_MANIFEST = "The generation manifest is malformed."
BAD_ARTIFACTS = "The generation artifact set is malformed."


StrSub = type("StrSubclass", (str,), {})
IntSub = type("IntSubclass", (int,), {})


class HostileKey(str):
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("hostile key comparison")


class HostileIdentity(GenerationProjectionIdentity):
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"hostile identity attribute read: {name}")


class HostileBinding(ResolvedProjectBinding):
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"hostile binding attribute read: {name}")


class HostileTarget(GenerationProjectionTarget):
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"hostile target attribute read: {name}")


# fmt: off
def _binding(project_id: str = PROJECT_ID, mode: str = "cloud") -> ResolvedProjectBinding:
    return ResolvedProjectBinding(Path("store"), project_id, "Nauro", mode,
        "https://mcp.nauro.ai" if mode == "cloud" else None)  # type: ignore[arg-type]


def _identity_values(raw: bytes, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "project_id": PROJECT_ID, "store_format_version": 1,
        "generation_id": GENERATION_ID, "manifest_digest": hashlib.sha256(raw).hexdigest(),
        "committed_at": COMMITTED_AT, "installed_for_user_id": USER_ID,
        "projection_class": "contributor_plus", "projection_scope_id": SCOPE_ID,
    }
    values.update(changes)
    return values


def _manifest_object(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "project_id": PROJECT_ID, "store_format_version": 1,
        "generation_id": GENERATION_ID, "committed_at": COMMITTED_AT,
        "projection_class": "contributor_plus",
        "projection_scope_id": SCOPE_ID,
        "artifacts": {"project.md": hashlib.sha256(CONTENT).hexdigest()},
    }
    body.update(changes)
    return body


def _manifest(**changes: object) -> bytes:
    return json.dumps(_manifest_object(**changes), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()
# fmt: on


def _target(raw: bytes, binding: ResolvedProjectBinding | None = None, **changes: object):
    identity = GenerationProjectionIdentity(**_identity_values(raw, **changes))
    return GenerationProjectionTarget(binding=binding or _binding(), identity=identity)


def _verify(raw: bytes | None = None, *, target=None, artifacts=(("project.md", CONTENT),)):
    raw = _manifest() if raw is None else raw
    return VerifiedGenerationProjection(target or _target(raw), raw, artifacts)


def _error(error_type: type[Exception], message: str, call) -> Exception:
    with pytest.raises(error_type) as raised:
        call()
    assert type(raised.value) is error_type
    expected = "generation_verification_failed"
    if error_type is ClientUpgradeRequiredError:
        expected = "client_upgrade_required"
    assert raised.value.code == expected
    assert str(raised.value) == message
    return raised.value


def _fails(message: str, call) -> Exception:
    error = _error(GenerationProjectionVerificationError, message, call)
    assert (error.__cause__, error.__context__) == (None, None)
    return error


def test_exact_public_fields_and_valid_target_bound_projection() -> None:
    assert set(GenerationProjectionIdentity.model_fields) == set(
        "project_id store_format_version generation_id manifest_digest committed_at "
        "installed_for_user_id projection_class projection_scope_id".split()
    )
    assert set(GenerationProjectionManifest.model_fields) == set(
        "project_id store_format_version generation_id committed_at projection_class "
        "projection_scope_id artifacts".split()
    )
    assert [item.name for item in fields(GenerationProjectionTarget)] == ["binding", "identity"]
    proof = _verify()
    assert (proof.target.binding.project_id, proof.target.identity.generation_id) == (
        PROJECT_ID,
        GENERATION_ID,
    )
    assert proof.manifest.committed_at == COMMITTED_AT


# fmt: off
@pytest.mark.parametrize("changes", [{"project_id": PROJECT_ID.lower()},{"store_format_version": 0},
    {"generation_id": "generation"}, {"manifest_digest": "A" * 64},
    {"committed_at": "2026-09-02T01:02:03Z"}, {"installed_for_user_id": "user"},
    {"projection_class": "owner"}, {"projection_scope_id": "a" * 63},
])
def test_identity_rejects_malformed_facts(changes: dict[str, object]) -> None:
    with pytest.raises(pyd.ValidationError):
        GenerationProjectionIdentity(**_identity_values(_manifest(), **changes))


@pytest.mark.parametrize("changes", [{"project_id": StrSub(PROJECT_ID)},
    {"store_format_version": IntSub(1)},
    {"generation_id": StrSub(GENERATION_ID)}, {"manifest_digest": StrSub("a" * 64)},
    {"committed_at": StrSub(COMMITTED_AT)}, {"installed_for_user_id": StrSub(USER_ID)},
    {"projection_class": StrSub("contributor_plus")}, {"projection_scope_id": StrSub(SCOPE_ID)},
])
def test_identity_before_validators_require_exact_types(changes: dict[str, object]) -> None:
    with pytest.raises(pyd.ValidationError):
        GenerationProjectionIdentity(**_identity_values(_manifest(), **changes))


def test_fully_valid_bypass_created_identity_is_rebuilt() -> None:
    raw = _manifest()
    values = _identity_values(raw)
    bypasses = (
        GenerationProjectionIdentity.model_construct(**values),
        GenerationProjectionIdentity(**values).model_copy(update={"generation_id": GENERATION_ID}),
    )
    rebuilt = [GenerationProjectionTarget(_binding(), item).identity for item in bypasses]
    assert rebuilt[0] == rebuilt[1]
    assert all(result is not source for result, source in zip(rebuilt, bypasses, strict=True))


@pytest.mark.parametrize("bypass", ["missing", "field-set", "extra", "invalid"])
def test_invalid_bypass_state_is_rejected(bypass: str) -> None:
    values = _identity_values(_manifest())
    if bypass == "missing":
        values.pop("generation_id")
        identity = GenerationProjectionIdentity.model_construct(**values)
    elif bypass == "field-set":
        identity = GenerationProjectionIdentity.model_construct(
            **values, _fields_set=set(values) - {"generation_id"}
        )
    elif bypass == "extra":
        identity = GenerationProjectionIdentity(**values).model_copy(update={"unexpected": "x"})
    else:
        identity = GenerationProjectionIdentity(**values).model_copy(
            update={"generation_id": False}
        )
    _fails(BAD_IDENTITY, lambda: GenerationProjectionTarget(_binding(), identity))


def test_hostile_subclasses_are_rejected_without_attribute_access() -> None:
    raw = _manifest()
    identity = HostileIdentity.model_construct(**_identity_values(raw))
    _fails(BAD_IDENTITY, lambda: GenerationProjectionTarget(_binding(), identity))
    binding = HostileBinding(Path("store"), PROJECT_ID, "Nauro", "cloud", "https://x")
    identity = GenerationProjectionIdentity(**_identity_values(raw))
    _fails(BAD_BINDING, lambda: GenerationProjectionTarget(binding, identity))
    hostile_target = object.__new__(HostileTarget)
    _fails(BAD_TARGET, lambda: _verify(raw, target=hostile_target))


@pytest.mark.parametrize(("owner", "field", "value", "message"), [
    ("target", "unexpected", "x", BAD_TARGET), ("target", "binding", ..., BAD_TARGET),
    ("target", "binding", None, BAD_BINDING), ("target", "identity", None, BAD_IDENTITY),
    ("binding", "unexpected", "x", BAD_BINDING), ("binding", "project_id", ..., BAD_BINDING),
    ("identity", "generation_id", ..., BAD_IDENTITY),
    ("identity", "__pydantic_fields_set__", ..., BAD_IDENTITY),
    ("identity", "__pydantic_extra__", ..., BAD_IDENTITY),
    ("target", "binding", HostileKey, BAD_TARGET),
    ("binding", "project_id", HostileKey, BAD_BINDING),
    ("identity", "project_id", HostileKey, BAD_IDENTITY),
    ("identity", "*project_id", HostileKey, BAD_IDENTITY),
    ("identity", "+__pydantic_fields_set__", None, BAD_IDENTITY),
])
def test_final_boundary_rejects_object_level_tampering(owner, field, value, message) -> None:
    raw, target = _manifest(), _target(_manifest())
    subject = target if owner == "target" else getattr(target, owner)
    if value is HostileKey:
        attribute = "__pydantic_fields_set__" if field.startswith("*") else "__dict__"
        field = field.removeprefix("*")
        state = dict.fromkeys(getattr(subject, attribute))
        state[HostileKey(field)] = state.pop(field)
        replacement = set(state) if attribute != "__dict__" else state
        object.__setattr__(subject, attribute, replacement)
    elif field.startswith("+"):
        field = field.removeprefix("+")
        object.__delattr__(subject, field)
        object.__setattr__(subject, "__pydantic_extra__", {HostileKey(field): None})
    elif value is ...:
        object.__delattr__(subject, field)
    else:
        object.__setattr__(subject, field, value)
    _fails(message, lambda: _verify(raw, target=target))
# fmt: on


def test_cloud_project_and_format_error_precedence() -> None:
    raw = _manifest()
    identity = GenerationProjectionIdentity(
        **_identity_values(raw, project_id=OTHER_PROJECT_ID, store_format_version=2)
    )
    _fails(BAD_BINDING, lambda: GenerationProjectionTarget(_binding("invalid"), identity))
    _fails(BAD_BINDING, lambda: GenerationProjectionTarget(_binding(mode="local"), identity))
    _fails(OTHER_PROJECT, lambda: GenerationProjectionTarget(_binding(), identity))
    _error(ClientUpgradeRequiredError, BAD_FORMAT, lambda: _target(raw, store_format_version=2))


def test_exact_manifest_bytes_digest_and_real_parse_failures() -> None:
    raw = _manifest()
    _fails(
        EXACT_MANIFEST,
        lambda: verify_generation_projection(
            _target(raw), manifest_json=type("BytesSubclass", (bytes,), {})(raw), artifacts=()
        ),
    )
    _fails(
        "The generation manifest digest diverges.",
        lambda: _verify(b"not-json", target=_target(raw), artifacts=object()),
    )
    for malformed in (b"", b"{", b"[]", b"{}"):
        _fails(BAD_MANIFEST, lambda malformed=malformed: _verify(malformed))


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_preflight_rejects_boms_and_non_utf8_before_pydantic(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    raw = _manifest().decode().encode(encoding)
    monkeypatch.setattr(
        GenerationProjectionManifest,
        "model_validate_json",
        staticmethod(lambda _raw: pytest.fail("Pydantic validation ran")),
    )
    _fails(BAD_MANIFEST, lambda: _verify(raw))


@pytest.mark.parametrize("kind", ["top", "nested", "escaped", "NaN", "Infinity", "-Infinity"])
def test_preflight_rejects_duplicate_keys_and_nonfinite_constants(kind: str) -> None:
    text = _manifest().decode()
    if kind == "top":
        text = text.replace('"generation_id":', '"generation_id":"x","generation_id":')
    elif kind == "nested":
        text = text.replace('"project.md":', '"project.md":"x","project.md":')
    elif kind == "escaped":
        text = text.replace('"generation_id":', '"generation_\\u0069d":"x","generation_id":')
    else:
        text = text.replace('"store_format_version":1', f'"store_format_version":{kind}')
    _fails(BAD_MANIFEST, lambda: _verify(text.encode()))


@pytest.mark.parametrize("error_type", [ValueError, TypeError, RecursionError])
def test_preflight_failures_are_generic(monkeypatch: pytest.MonkeyPatch, error_type) -> None:
    monkeypatch.setattr(
        generation_projection.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error_type("secret")),
    )
    error = _fails(BAD_MANIFEST, lambda: _verify())
    assert "secret" not in str(error)


def test_preflight_result_is_discarded_and_pydantic_receives_original_bytes(monkeypatch) -> None:
    raw, seen = _manifest(), []
    original = GenerationProjectionManifest.model_validate_json
    monkeypatch.setattr(generation_projection.json, "loads", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        GenerationProjectionManifest,
        "model_validate_json",
        staticmethod(lambda value: seen.append(value) or original(value)),
    )
    _verify(raw)
    assert seen == [raw] and seen[0] is raw


# fmt: off
@pytest.mark.parametrize("raw", [
    json.dumps(_manifest_object(), sort_keys=False).encode(),
    json.dumps(_manifest_object(), sort_keys=True, indent=2).encode(), _manifest() + b"\n",
    json.dumps(_manifest_object(artifacts={"decisions/caf\u00e9.md": "b" * 64}),
        sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(),
])
def test_manifest_requires_canonical_compact_sorted_utf8(raw: bytes) -> None:
    _fails("The generation manifest is not canonical.", lambda: _verify(raw, artifacts=()))


def test_invalid_path_and_lone_surrogate_fail_as_malformed_manifest() -> None:
    invalid_path = _manifest(artifacts={"unknown.md": "b" * 64})
    _fails(BAD_MANIFEST, lambda: _verify(invalid_path, artifacts=()))
    raw = json.dumps(_manifest_object(artifacts={"decisions/\ud800.md": "b" * 64}),
        sort_keys=True, separators=(",", ":")).encode()
    _fails(BAD_MANIFEST, lambda: _verify(raw, artifacts=()))


@pytest.mark.parametrize("changes", [
    {"project_id": OTHER_PROJECT_ID}, {"store_format_version": 2},
    {"generation_id": "01K22222222222222222222222"},
    {"committed_at": "2998-12-31T23:59:59.999999Z"}, {"projection_class": "viewer"},
    {"projection_scope_id": "b" * 64},
])
def test_envelope_must_match_target(changes: dict[str, object]) -> None:
    raw = _manifest(**changes)
    _fails("The generation manifest does not match the projection target.",
        lambda: _verify(raw, target=_target(raw)))


def test_d501_role_seam_precedes_artifact_handling() -> None:
    content = b"{}"
    raw = _manifest(projection_class="viewer",
        artifacts=_manifest_object()["artifacts"]
        | {"questions-provenance.json": hashlib.sha256(content).hexdigest()})
    _fails(
        "Viewer generation projections cannot include questions-provenance.json.",
        lambda: _verify(raw, target=_target(raw, projection_class="viewer"), artifacts=object()),
    )
    contributor = _manifest(
        artifacts={"questions-provenance.json": hashlib.sha256(content).hexdigest()})
    assert _verify(contributor, artifacts=(("questions-provenance.json", content),)).artifacts


@pytest.mark.parametrize("changes", [
    {"project_id": False}, {"store_format_version": False}, {"projection_class": False},
    {"committed_at": False}, {"committed_at": "2026-09-02T01:02:03Z"},
    {"committed_at": StrSub(COMMITTED_AT)},
    {"artifacts": []}, {"artifacts": type("DictSubclass", (dict,), {})({"project.md": "b" * 64})},
    {"artifacts": {StrSub("project.md"): "b" * 64}},
    {"artifacts": {"project.md": StrSub("b" * 64)}}, {"snapshot_digest": "b" * 64},
])
def test_manifest_direct_validation_is_exact_typed_and_closed(changes) -> None:
    with pytest.raises(pyd.ValidationError):
        GenerationProjectionManifest(**_manifest_object(**changes))


@pytest.mark.parametrize("artifacts", [
    (item for item in (("project.md", CONTENT),)), (("project.md", CONTENT, "a" * 64),),
    [("project.md", CONTENT)], type("TupleSubclass", (tuple,), {})((("project.md", CONTENT),)),
    (["project.md", CONTENT],), (type("PairSubclass", (tuple,), {})(("project.md", CONTENT)),),
    ((StrSub("project.md"), CONTENT),), (("project.md", type("B", (bytes,), {})(CONTENT)),),
    (("project.md", bytearray(CONTENT)),), (("project.md",),),
    (VerifiedGenerationArtifact("project.md", CONTENT),),
])
def test_artifact_boundary_requires_exact_tuple_pair_str_and_bytes(artifacts) -> None:
    _fails(BAD_ARTIFACTS, lambda: _verify(artifacts=artifacts))


@pytest.mark.parametrize(("artifacts", "message"), [
    ((("unknown.md", CONTENT), ("project.md",)), BAD_ARTIFACTS),
    ((("unknown.md", CONTENT), ("unknown.md", CONTENT)),
        "The generation contains an invalid protected artifact path."),
    ((("project.md", CONTENT), ("project.md", CONTENT)),
        "The generation contains duplicate artifact paths."),
    ((), "The generation artifact set differs from the manifest."),
    ((("project.md", CONTENT), ("state.md", b"x")),
        "The generation artifact set differs from the manifest."),
    ((("project.md", b"divergent"),), "The generation artifact digest diverges: project.md."),
])
def test_artifact_error_precedence_and_fresh_digest(artifacts, message: str) -> None:
    _fails(message, lambda: _verify(artifacts=artifacts))


def test_direct_construction_repeats_complete_boundary_and_rebuilds() -> None:
    artifact = VerifiedGenerationArtifact("project.md", b"divergent")
    object.__setattr__(artifact, "digest", hashlib.sha256(CONTENT).hexdigest())
    _fails(BAD_ARTIFACTS, lambda: _verify(artifacts=(artifact,)))
    raw, target = _manifest(), _target(_manifest())
    direct = VerifiedGenerationProjection(target, raw, (("project.md", CONTENT),))
    helper = verify_generation_projection(target, manifest_json=raw,
        artifacts=(("project.md", CONTENT),))
    assert direct == helper and direct.target is not target
    assert direct.target.identity is not target.identity
    assert direct.manifest is not helper.manifest
    assert direct.artifacts[0] is not helper.artifacts[0]
    _fails("The generation artifact digest diverges: project.md.",
        lambda: VerifiedGenerationProjection(target, raw, (("project.md", b"bad"),)))
# fmt: on


def test_proof_is_deeply_immutable_path_sorted_and_accepts_empty() -> None:
    state = b"# State\n"
    raw = _manifest(
        artifacts={
            "state_current.md": hashlib.sha256(state).hexdigest(),
            "project.md": hashlib.sha256(CONTENT).hexdigest(),
        }
    )
    proof = _verify(raw, artifacts=(("state_current.md", state), ("project.md", CONTENT)))
    assert tuple(item.path for item in proof.artifacts) == ("project.md", "state_current.md")
    assert isinstance(proof.artifacts_by_path, MappingProxyType)
    with pytest.raises(FrozenInstanceError):
        proof.manifest_json = b"{}"
    with pytest.raises(FrozenInstanceError):
        proof.artifacts[0].content = b"changed"
    with pytest.raises(TypeError):
        proof.manifest.artifacts["project.md"] = "b" * 64
    empty_raw = _manifest(artifacts={})
    empty = _verify(empty_raw, artifacts=())
    assert empty.artifacts == () and dict(empty.artifacts_by_path) == {}
