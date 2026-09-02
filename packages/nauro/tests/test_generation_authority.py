from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pydantic as pyd
import pytest

from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    FlatProjectAuthority,
    GenerationControlCorruptError,
    GenerationProjectAuthority,
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


def _binding(
    *,
    mode: str = "cloud",
    project_id: str = PROJECT_ID,
) -> ResolvedProjectBinding:
    return ResolvedProjectBinding(
        store_path=Path("store"),
        project_id=project_id,
        display_name="Nauro",
        mode=mode,  # type: ignore[arg-type]
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


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_absent_marker_selects_flat_authority_and_ignores_dormant_pointer(mode: str) -> None:
    binding = _binding(mode=mode)

    result = select_project_authority(
        binding,
        marker_json=None,
        pointer_json="not-json",
    )

    assert result == FlatProjectAuthority(binding)
    assert result.kind == ("local" if mode == "local" else "hosted_legacy")


def test_local_project_refuses_generation_marker() -> None:
    with pytest.raises(GenerationControlCorruptError) as raised:
        select_project_authority(
            _binding(mode="local"),
            marker_json=_marker(),
            pointer_json=_pointer(),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )

    assert raised.value.code == "generation_control_corrupt"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "{}",
        b"\xff",
        json.dumps(
            {
                "schema_version": 1,
                "authority": "generation",
                "project_id": PROJECT_ID,
                "store_format_version": 1,
                "extra": True,
            }
        ),
        _marker(schema_version=False),
        _marker(schema_version=2),
        _marker(authority="legacy"),
        _marker(project_id=PROJECT_ID.lower()),
        _marker(store_format_version=False),
        _marker(store_format_version=0),
    ],
)
def test_marker_must_be_one_strict_closed_json_object(raw: str | bytes) -> None:
    with pytest.raises(GenerationControlCorruptError) as raised:
        select_project_authority(
            _binding(),
            marker_json=raw,
            pointer_json=_pointer(),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )

    assert raised.value.code == "generation_control_corrupt"


def test_marker_rejects_non_json_input_type() -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json={"authority": "generation"},  # type: ignore[arg-type]
            pointer_json=_pointer(),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


def test_unsupported_marker_format_requires_client_upgrade_before_pointer_read() -> None:
    with pytest.raises(ClientUpgradeRequiredError) as raised:
        select_project_authority(
            _binding(),
            marker_json=_marker(store_format_version=2),
            pointer_json="not-json",
        )

    assert raised.value.code == "client_upgrade_required"


def test_marker_must_match_bound_project() -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json=_marker(project_id=OTHER_PROJECT_ID),
            pointer_json=_pointer(project_id=OTHER_PROJECT_ID),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


def test_active_marker_without_account_reports_actor_mismatch_before_pointer() -> None:
    with pytest.raises(ReplicaActorMismatchError) as raised:
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=None,
        )

    assert raised.value.code == "replica_actor_mismatch"


def test_active_account_without_pointer_requires_refresh() -> None:
    with pytest.raises(RefreshRequiredError) as raised:
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=None,
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )

    assert raised.value.code == "refresh_required"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "{}",
        b"\xff",
        json.dumps(
            {
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
                "extra": True,
            }
        ),
    ],
)
def test_pointer_must_be_one_strict_closed_json_object(raw: str | bytes) -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=raw,
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", False),
        ("schema_version", 2),
        ("project_id", PROJECT_ID.lower()),
        ("store_format_version", False),
        ("store_format_version", 0),
        ("generation_id", "generation-1"),
        ("manifest_digest", "A" * 64),
        ("manifest_digest", "a" * 63),
        ("committed_at", "2026-09-02T01:02:03Z"),
        ("committed_at", "2026-99-02T01:02:03.000004Z"),
        ("installed_at", "2026-09-02T01:03:04.00005Z"),
        ("installed_at", "2026-09-02T01:02:02.999999Z"),
        ("installed_state_id", "state-1"),
        ("installed_for_user_id", "user-1"),
        ("projection_class", "owner"),
        ("projection_scope_id", "B" * 64),
        ("projection_scope_id", "b" * 65),
    ],
)
def test_pointer_fields_are_strict_and_canonical(field: str, value: object) -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=_pointer(**{field: value}),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


def test_pointer_rejects_non_json_input_type() -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json={"project_id": PROJECT_ID},  # type: ignore[arg-type]
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": OTHER_PROJECT_ID},
        {"store_format_version": 2},
    ],
)
def test_pointer_must_cross_bind_to_marker_and_project(changes: dict[str, object]) -> None:
    with pytest.raises(GenerationControlCorruptError):
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=_pointer(**changes),
            active_user_id=USER_ID,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )


@pytest.mark.parametrize("active_user_id", [None, "user-1", OTHER_USER_ID])
def test_generation_selection_requires_matching_active_actor(
    active_user_id: str | None,
) -> None:
    with pytest.raises(ReplicaActorMismatchError) as raised:
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=_pointer(),
            active_user_id=active_user_id,
            active_projection_scope_id=PROJECTION_SCOPE_ID,
        )

    assert raised.value.code == "replica_actor_mismatch"


@pytest.mark.parametrize(
    "active_scope",
    [None, "scope-1", "B" * 64, OTHER_PROJECTION_SCOPE_ID],
)
def test_generation_selection_requires_matching_authorization_scope(
    active_scope: str | None,
) -> None:
    with pytest.raises(RefreshRequiredError) as raised:
        select_project_authority(
            _binding(),
            marker_json=_marker(),
            pointer_json=_pointer(),
            active_user_id=USER_ID,
            active_projection_scope_id=active_scope,
        )

    assert raised.value.code == "refresh_required"


def test_valid_generation_selection_retains_all_bound_facts() -> None:
    binding = _binding()

    result = select_project_authority(
        binding,
        marker_json=_marker(),
        pointer_json=_pointer(),
        active_user_id=USER_ID,
        active_projection_scope_id=PROJECTION_SCOPE_ID,
    )

    assert isinstance(result, GenerationProjectAuthority)
    assert result.kind == "hosted_generation"
    assert result.binding is binding
    assert result.marker.project_id == PROJECT_ID
    assert result.marker.store_format_version == 1
    assert result.pointer.project_id == PROJECT_ID
    assert result.pointer.generation_id == GENERATION_ID
    assert result.pointer.manifest_digest == MANIFEST_DIGEST
    assert result.pointer.committed_at == COMMITTED_AT
    assert result.pointer.installed_at == INSTALLED_AT
    assert result.pointer.installed_state_id == INSTALL_STATE_ID
    assert result.pointer.installed_for_user_id == USER_ID
    assert result.pointer.projection_class == "contributor_plus"
    assert result.pointer.projection_scope_id == PROJECTION_SCOPE_ID

    with pytest.raises(pyd.ValidationError):
        result.pointer.generation_id = OTHER_PROJECT_ID
    with pytest.raises(FrozenInstanceError):
        result.binding = _binding(project_id=OTHER_PROJECT_ID)
