from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Literal

import pydantic as pyd
import pytest

from nauro.store import generation_authority
from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    FlatProjectAuthority,
    GenerationAuthorityMarker,
    GenerationControlCorruptError,
    GenerationProjectAuthority,
    InstalledGenerationPointer,
    RefreshRequiredError,
    ReplicaActorMismatchError,
    select_project_authority,
)
from nauro.store.resolution import ResolvedProjectBinding

PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
OTHER_PROJECT_ID = "01K00000000000000000000000"
GENERATION_ID = "01K11111111111111111111111"
INSTALL_STATE_ID = "01K22222222222222222222222"
USER_ID = "01K33333333333333333333333"
OTHER_USER_ID = "01K44444444444444444444444"
MANIFEST_DIGEST = "a" * 64
PROJECTION_SCOPE_ID = "b" * 64
OTHER_PROJECTION_SCOPE_ID = "c" * 64
COMMITTED_AT = "2026-09-02T01:02:03.000004Z"
INSTALLED_AT = "2026-09-02T01:03:04.000005Z"


class HostileStr(str):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile string was compared")

    def __len__(self) -> int:
        raise AssertionError("hostile string length was read")

    def __iter__(self):
        raise AssertionError("hostile string was iterated")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("hostile string was encoded")


def _binding(
    *,
    mode: Literal["local", "cloud"] = "cloud",
    project_id: str = PROJECT_ID,
) -> ResolvedProjectBinding:
    return ResolvedProjectBinding(
        store_path=Path("store"),
        project_id=project_id,
        display_name="Nauro",
        mode=mode,
        server_url="https://mcp.nauro.ai" if mode == "cloud" else None,
    )


def _marker(**changes: object) -> str:
    raw: dict[str, object] = {
        "schema_version": 1,
        "authority": "generation",
        "project_id": PROJECT_ID,
        "store_format_version": 1,
    }
    raw.update(changes)
    return json.dumps(raw)


def _pointer(**changes: object) -> str:
    raw: dict[str, object] = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": GENERATION_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "committed_at": COMMITTED_AT,
        "installed_at": INSTALLED_AT,
        "installed_state_id": INSTALL_STATE_ID,
        "installed_for_user_id": USER_ID,
        "projection_class": "contributor_plus",
        "projection_scope_id": PROJECTION_SCOPE_ID,
    }
    raw.update(changes)
    return json.dumps(raw)


def _select(
    *,
    marker_json: str | bytes | None = None,
    pointer_json: str | bytes | None = None,
    active_user_id: str | None = USER_ID,
    active_scope_id: str | None = PROJECTION_SCOPE_ID,
    binding: ResolvedProjectBinding | None = None,
):
    return select_project_authority(
        binding or _binding(),
        marker_json=marker_json,
        pointer_json=pointer_json,
        active_user_id=active_user_id,
        active_projection_scope_id=active_scope_id,
    )


def _assert_corrupt(
    *,
    boundary: Literal["marker", "pointer"],
    raw: object,
) -> GenerationControlCorruptError:
    kwargs = {
        "marker_json": raw if boundary == "marker" else _marker(),
        "pointer_json": raw if boundary == "pointer" else _pointer(),
    }
    with pytest.raises(GenerationControlCorruptError) as raised:
        _select(**kwargs)  # type: ignore[arg-type]
    assert type(raised.value) is GenerationControlCorruptError
    assert raised.value.code == "generation_control_corrupt"
    return raised.value


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_absent_marker_selects_flat_authority_and_ignores_dormant_inputs(
    mode: Literal["local", "cloud"],
) -> None:
    binding = _binding(mode=mode)

    result = select_project_authority(
        binding,
        marker_json=None,
        pointer_json=HostileStr("not-json"),
        active_user_id=HostileStr("invalid"),
        active_projection_scope_id=HostileStr("invalid"),
    )

    assert result == FlatProjectAuthority(binding)
    assert result.kind == ("local" if mode == "local" else "hosted_legacy")


def test_local_project_refuses_generation_marker() -> None:
    with pytest.raises(GenerationControlCorruptError) as raised:
        _select(binding=_binding(mode="local"), marker_json=_marker(), pointer_json=_pointer())

    assert type(raised.value) is GenerationControlCorruptError
    assert raised.value.code == "generation_control_corrupt"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "{}",
        b"\xff",
        _marker(extra=True),
        _marker(schema_version=False),
        _marker(schema_version=2),
        _marker(authority="legacy"),
        _marker(authority=1),
        _marker(project_id=PROJECT_ID.lower()),
        _marker(project_id=1),
        _marker(store_format_version=False),
        _marker(store_format_version=0),
        _marker(store_format_version=1.0),
    ],
)
def test_marker_must_be_one_strict_closed_canonical_object(raw: str | bytes) -> None:
    _assert_corrupt(boundary="marker", raw=raw)


def test_marker_must_match_bound_project() -> None:
    _assert_corrupt(boundary="marker", raw=_marker(project_id=OTHER_PROJECT_ID))


@pytest.mark.parametrize(
    ("boundary", "raw"),
    [
        ("marker", {"authority": "generation"}),
        ("pointer", {"project_id": PROJECT_ID}),
        pytest.param("marker", HostileStr(_marker()), id="marker-hostile-str"),
        pytest.param("pointer", HostileStr(_pointer()), id="pointer-hostile-str"),
    ],
)
def test_control_json_requires_exact_builtin_input_types(
    boundary: Literal["marker", "pointer"], raw: object
) -> None:
    _assert_corrupt(boundary=boundary, raw=raw)


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_control_json_rejects_bom_and_non_utf8_encodings_before_pydantic(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
    encoding: str,
) -> None:
    model = GenerationAuthorityMarker if boundary == "marker" else InstalledGenerationPointer
    raw = (_marker() if boundary == "marker" else _pointer()).encode(encoding)
    monkeypatch.setattr(
        model,
        "model_validate_json",
        staticmethod(lambda _raw: pytest.fail("Pydantic validation ran")),
    )

    _assert_corrupt(boundary=boundary, raw=raw)


def _duplicate_json(boundary: str, location: str) -> str:
    raw = _marker() if boundary == "marker" else _pointer()
    if location == "top-level":
        return raw.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
    if location == "escaped":
        return raw.replace('"schema_version": 1', '"schema_version": 2, "schema_\\u0076ersion": 1')
    return raw[:-1] + ', "nested": {"secret_duplicate": 1, "secret_duplicate": 2}}'


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
@pytest.mark.parametrize("location", ["top-level", "nested", "escaped"])
def test_control_json_rejects_duplicate_keys_before_pydantic_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
    location: str,
) -> None:
    model = GenerationAuthorityMarker if boundary == "marker" else InstalledGenerationPointer
    monkeypatch.setattr(
        model,
        "model_validate_json",
        staticmethod(lambda _raw: pytest.fail("Pydantic validation ran")),
    )

    error = _assert_corrupt(boundary=boundary, raw=_duplicate_json(boundary, location))

    assert "schema_version" not in str(error)
    assert "secret_duplicate" not in str(error)


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_control_json_rejects_nonfinite_constants_before_pydantic_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
    constant: str,
) -> None:
    model = GenerationAuthorityMarker if boundary == "marker" else InstalledGenerationPointer
    raw = (_marker() if boundary == "marker" else _pointer()).replace(
        '"schema_version": 1', f'"schema_version": {constant}'
    )
    monkeypatch.setattr(
        model,
        "model_validate_json",
        staticmethod(lambda _raw: pytest.fail("Pydantic validation ran")),
    )

    error = _assert_corrupt(boundary=boundary, raw=raw)

    assert constant not in str(error)


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
def test_control_json_maps_deep_recursion_to_generic_error(
    boundary: Literal["marker", "pointer"],
) -> None:
    raw = "[" * 1100 + "0" + "]" * 1100

    error = _assert_corrupt(boundary=boundary, raw=raw)

    assert str(error) in {
        "The generation authority marker is invalid.",
        "The installed generation pointer is invalid.",
    }


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, RecursionError])
def test_control_json_maps_preflight_failures_to_generic_error(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
    error_type: type[Exception],
) -> None:
    target = _marker() if boundary == "marker" else _pointer()
    original = generation_authority._strict_json_preflight

    def fail_target(raw: str | bytes) -> None:
        if raw is target:
            raise error_type("secret parser detail")
        original(raw)

    monkeypatch.setattr(generation_authority, "_strict_json_preflight", fail_target)

    error = _assert_corrupt(boundary=boundary, raw=target)

    assert "secret parser detail" not in str(error)


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, RecursionError])
def test_control_json_maps_pydantic_failures_to_generic_error(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
    error_type: type[Exception],
) -> None:
    model = GenerationAuthorityMarker if boundary == "marker" else InstalledGenerationPointer
    monkeypatch.setattr(
        model,
        "model_validate_json",
        staticmethod(lambda _raw: (_ for _ in ()).throw(error_type("secret validator detail"))),
    )

    error = _assert_corrupt(
        boundary=boundary, raw=_marker() if boundary == "marker" else _pointer()
    )

    assert "secret validator detail" not in str(error)


@pytest.mark.parametrize("boundary", ["marker", "pointer"])
def test_pydantic_validates_the_original_json_input(
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["marker", "pointer"],
) -> None:
    model = GenerationAuthorityMarker if boundary == "marker" else InstalledGenerationPointer
    original = model.model_validate_json
    raw = (_marker() if boundary == "marker" else _pointer()).encode()
    seen: list[str | bytes] = []

    def capture(value: str | bytes):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(model, "model_validate_json", staticmethod(capture))

    _select(
        marker_json=raw if boundary == "marker" else _marker(),
        pointer_json=raw if boundary == "pointer" else _pointer(),
    )

    assert seen == [raw]
    assert seen[0] is raw


def test_unsupported_marker_format_wins_before_actor_and_pointer_handling() -> None:
    with pytest.raises(ClientUpgradeRequiredError) as raised:
        _select(
            marker_json=_marker(store_format_version=2),
            pointer_json=HostileStr("not-json"),
            active_user_id=None,
            active_scope_id=None,
        )

    assert type(raised.value) is ClientUpgradeRequiredError
    assert raised.value.code == "client_upgrade_required"


@pytest.mark.parametrize(
    "active_user_id",
    [None, 1, "user-1", OTHER_USER_ID, pytest.param(HostileStr(USER_ID), id="hostile-str")],
)
def test_generation_selection_requires_exact_matching_active_actor(
    active_user_id: object,
) -> None:
    with pytest.raises(ReplicaActorMismatchError) as raised:
        _select(
            marker_json=_marker(),
            pointer_json=_pointer(),
            active_user_id=active_user_id,  # type: ignore[arg-type]
        )

    assert type(raised.value) is ReplicaActorMismatchError
    assert raised.value.code == "replica_actor_mismatch"


def test_actor_mismatch_wins_before_pointer_handling() -> None:
    with pytest.raises(ReplicaActorMismatchError):
        _select(
            marker_json=_marker(),
            pointer_json=HostileStr("not-json"),
            active_user_id=HostileStr(USER_ID),
        )


def test_valid_actor_without_pointer_requires_refresh() -> None:
    with pytest.raises(RefreshRequiredError) as raised:
        _select(marker_json=_marker(), pointer_json=None)

    assert type(raised.value) is RefreshRequiredError
    assert raised.value.code == "refresh_required"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "{}",
        b"\xff",
        _pointer(extra=True),
    ],
)
def test_pointer_must_be_one_strict_closed_object(raw: str | bytes) -> None:
    _assert_corrupt(boundary="pointer", raw=raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", False),
        ("schema_version", 2),
        ("project_id", PROJECT_ID.lower()),
        ("store_format_version", False),
        ("store_format_version", 0),
        ("store_format_version", 1.0),
        ("generation_id", "generation-1"),
        ("manifest_digest", "A" * 64),
        ("manifest_digest", "a" * 63),
        ("committed_at", "2026-09-02T01:02:03Z"),
        ("committed_at", "2026-99-02T01:02:03.000004Z"),
        ("installed_at", "2026-09-02T01:03:04.00005Z"),
        ("installed_at", "2026-99-02T01:03:04.000005Z"),
        ("installed_state_id", "state-1"),
        ("installed_for_user_id", "user-1"),
        ("projection_class", "owner"),
        ("projection_scope_id", "B" * 64),
        ("projection_scope_id", "b" * 65),
    ],
)
def test_pointer_fields_are_strict_and_canonical(field: str, value: object) -> None:
    _assert_corrupt(boundary="pointer", raw=_pointer(**{field: value}))


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": OTHER_PROJECT_ID},
        {"store_format_version": 2},
    ],
)
def test_pointer_must_cross_bind_to_marker_and_project(changes: dict[str, object]) -> None:
    _assert_corrupt(boundary="pointer", raw=_pointer(**changes))


def test_pointer_actor_must_match_the_valid_active_actor() -> None:
    with pytest.raises(ReplicaActorMismatchError) as raised:
        _select(
            marker_json=_marker(),
            pointer_json=_pointer(installed_for_user_id=OTHER_USER_ID),
        )

    assert type(raised.value) is ReplicaActorMismatchError
    assert raised.value.code == "replica_actor_mismatch"


@pytest.mark.parametrize(
    "active_scope",
    [
        None,
        1,
        "scope-1",
        "B" * 64,
        OTHER_PROJECTION_SCOPE_ID,
        pytest.param(HostileStr(PROJECTION_SCOPE_ID), id="hostile-str"),
    ],
)
def test_generation_selection_requires_exact_matching_authorization_scope(
    active_scope: object,
) -> None:
    with pytest.raises(RefreshRequiredError) as raised:
        _select(
            marker_json=_marker(),
            pointer_json=_pointer(),
            active_scope_id=active_scope,  # type: ignore[arg-type]
        )

    assert type(raised.value) is RefreshRequiredError
    assert raised.value.code == "refresh_required"


def test_pointer_does_not_compare_server_and_local_clocks() -> None:
    installed_before_commit = "2026-09-02T01:02:02.999999Z"

    result = _select(
        marker_json=_marker(),
        pointer_json=_pointer(installed_at=installed_before_commit),
    )

    assert isinstance(result, GenerationProjectAuthority)
    assert result.pointer.committed_at == COMMITTED_AT
    assert result.pointer.installed_at == installed_before_commit


def test_valid_generation_selection_retains_all_bound_facts_and_is_immutable() -> None:
    binding = _binding()

    result = _select(binding=binding, marker_json=_marker(), pointer_json=_pointer())

    assert isinstance(result, GenerationProjectAuthority)
    assert result.kind == "hosted_generation"
    assert result.binding is binding
    assert result.marker.model_dump() == {
        "schema_version": 1,
        "authority": "generation",
        "project_id": PROJECT_ID,
        "store_format_version": 1,
    }
    assert result.pointer.model_dump() == {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": GENERATION_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "committed_at": COMMITTED_AT,
        "installed_at": INSTALLED_AT,
        "installed_state_id": INSTALL_STATE_ID,
        "installed_for_user_id": USER_ID,
        "projection_class": "contributor_plus",
        "projection_scope_id": PROJECTION_SCOPE_ID,
    }
    with pytest.raises(pyd.ValidationError):
        result.pointer.generation_id = OTHER_PROJECT_ID
    with pytest.raises(FrozenInstanceError):
        result.binding = _binding(project_id=OTHER_PROJECT_ID)
