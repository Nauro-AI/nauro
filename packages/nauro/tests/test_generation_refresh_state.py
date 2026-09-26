import json

import pytest

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_refresh_state import (
    _MAX_CONTROL_BYTES,
    GenerationRefreshEvidenceError,
    RefreshControlPair,
)
from tests.test_generation_installation import (
    OTHER_COMMITTED_AT,
    _authorization_bytes,
    _pointer_bytes,
    _projection,
)


def test_control_pair_requires_matching_pointer_and_authorization():
    projection = _projection()
    other = _projection(committed_at=OTHER_COMMITTED_AT)
    pair = RefreshControlPair(_pointer_bytes(projection), _authorization_bytes(projection))
    assert pair.pointer_json == _pointer_bytes(projection)
    RefreshControlPair(_pointer_bytes(other), _authorization_bytes(other))
    with pytest.raises(GenerationRefreshEvidenceError, match="do not form a pair"):
        RefreshControlPair(_pointer_bytes(projection), _authorization_bytes(other))


@pytest.mark.parametrize("field", ["pointer_json", "authorization_json"])
def test_control_pair_refuses_oversized_records(field):
    projection = _projection()
    records = {
        "pointer_json": _pointer_bytes(projection),
        "authorization_json": _authorization_bytes(projection),
    }
    records[field] += b" " * (_MAX_CONTROL_BYTES + 1 - len(records[field]))
    assert len(records[field]) == _MAX_CONTROL_BYTES + 1
    with pytest.raises(GenerationRefreshEvidenceError, match="bounded bytes"):
        RefreshControlPair(**records)


@pytest.mark.parametrize("kind", ["not_json", "empty_object", "authorization_shape"])
def test_control_pair_refuses_malformed_pointer_records(kind):
    projection = _projection()
    pointer = {
        "not_json": b"pointer",
        "empty_object": b"{}",
        "authorization_shape": _authorization_bytes(projection),
    }[kind]
    with pytest.raises(GenerationAuthorityError):
        RefreshControlPair(pointer, _authorization_bytes(projection))


@pytest.mark.parametrize("field", ["pointer_json", "authorization_json"])
def test_control_pair_refuses_noncanonical_records(field):
    projection = _projection()
    records = {
        "pointer_json": _pointer_bytes(projection),
        "authorization_json": _authorization_bytes(projection),
    }
    records[field] = json.dumps(json.loads(records[field]), indent=1).encode()
    with pytest.raises(GenerationAuthorityError):
        RefreshControlPair(**records)


@pytest.mark.parametrize("raw", [None, b""])
@pytest.mark.parametrize("field", ["pointer_json", "authorization_json"])
def test_control_pair_refuses_missing_or_empty_records(field, raw):
    projection = _projection()
    records = {
        "pointer_json": _pointer_bytes(projection),
        "authorization_json": _authorization_bytes(projection),
    }
    records[field] = raw
    with pytest.raises(GenerationRefreshEvidenceError, match="bounded bytes"):
        RefreshControlPair(**records)
