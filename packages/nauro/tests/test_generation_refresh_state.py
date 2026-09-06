from dataclasses import replace
from itertools import product

import pytest

from nauro.store.generation_authority import GenerationAuthorityError, GenerationAuthorityMarker
from nauro.store.generation_refresh_state import (
    GenerationRefreshEvidenceError,
    RefreshControlPair,
    RefreshControlTransition,
    classify_refresh_control,
)
from tests.test_generation_installation import (
    PROJECT_ID,
    _authorization_bytes,
    _pointer_bytes,
    _projection,
)


@pytest.fixture
def transition():
    projection = _projection()
    base = RefreshControlPair(_pointer_bytes(projection), _authorization_bytes(projection))
    changes = {
        "installed_state_id": "01K55555555555555555555555",
        "generation_id": "01K66666666666666666666666",
    }
    target = RefreshControlPair(
        _pointer_bytes(projection, **changes), _authorization_bytes(projection, **changes)
    )
    marker = GenerationAuthorityMarker(
        schema_version=1, authority="generation", project_id=PROJECT_ID, store_format_version=1
    ).canonical_bytes()
    return RefreshControlTransition(marker, base, target)


@pytest.mark.parametrize("pointer,carrier", list(product(["base", "target", "other"], repeat=2)))
def test_classify_all_pair_interleavings(transition, pointer, carrier):
    other = _projection()
    pointers = {
        "base": transition.base.pointer_json,
        "target": transition.target.pointer_json,
        "other": _pointer_bytes(other, installed_state_id="01K77777777777777777777777"),
    }
    carriers = {
        "base": transition.base.authorization_json,
        "target": transition.target.authorization_json,
        "other": _authorization_bytes(other, installed_state_id="01K77777777777777777777777"),
    }
    expected = {
        ("base", "base"): "base_present",
        ("base", "target"): "carrier_published",
        ("target", "target"): "target_present",
    }.get((pointer, carrier), "conflict")
    assert (
        classify_refresh_control(
            transition,
            marker_json=transition.marker_json,
            pointer_json=pointers[pointer],
            authorization_json=carriers[carrier],
        )
        == expected
    )


@pytest.mark.parametrize("field", ["pointer_json", "authorization_json", "marker_json"])
@pytest.mark.parametrize("raw", [None, b"", b"{}", b"x" * (16 * 1024 + 1)])
def test_invalid_observation_is_not_absence_or_success(transition, field, raw):
    observation = {
        "pointer_json": transition.target.pointer_json,
        "authorization_json": transition.target.authorization_json,
        "marker_json": transition.marker_json,
    }
    observation[field] = raw
    with pytest.raises(GenerationAuthorityError):
        classify_refresh_control(transition, **observation)


def test_noncanonical_observation_refuses(transition):
    with pytest.raises(GenerationRefreshEvidenceError):
        classify_refresh_control(
            transition,
            marker_json=transition.marker_json,
            pointer_json=transition.base.pointer_json + b"\n",
            authorization_json=transition.base.authorization_json,
        )


def test_transition_requires_complete_pairs_and_new_install_identity(transition):
    with pytest.raises(GenerationRefreshEvidenceError):
        RefreshControlPair(transition.base.pointer_json, transition.target.authorization_json)
    with pytest.raises(GenerationRefreshEvidenceError):
        replace(transition, target=transition.base)


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "01K88888888888888888888888"),
        ("installed_for_user_id", "01K88888888888888888888888"),
        ("store_format_version", 2),
    ],
)
def test_transition_cannot_cross_binding(transition, field, value):
    projection = _projection()
    changes = {field: value, "installed_state_id": "01K99999999999999999999999"}
    target = RefreshControlPair(
        _pointer_bytes(projection, **changes), _authorization_bytes(projection, **changes)
    )
    with pytest.raises(GenerationRefreshEvidenceError):
        replace(transition, target=target)


def test_changed_marker_conflicts(transition):
    marker = GenerationAuthorityMarker(
        schema_version=1,
        authority="generation",
        project_id="01K88888888888888888888888",
        store_format_version=1,
    )
    assert (
        classify_refresh_control(
            transition,
            marker_json=marker.canonical_bytes(),
            pointer_json=transition.target.pointer_json,
            authorization_json=transition.target.authorization_json,
        )
        == "conflict"
    )
