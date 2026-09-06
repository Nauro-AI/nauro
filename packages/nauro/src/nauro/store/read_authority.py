from __future__ import annotations

from nauro_core.constants import HOSTED_STORE_FORMAT_VERSION

from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    GenerationControlCorruptError,
    _parse_marker,
)
from nauro.store.replica_control import (
    _read_optional_file,
    _validate_managed_path,
    _validate_store_path,
)
from nauro.store.resolution import ResolvedProjectBinding


def observe_generation_marker(binding: ResolvedProjectBinding) -> bytes | None:
    store = _validate_store_path(binding)
    path = store / ".replica" / "authority.json"
    _validate_managed_path(store, path)
    raw = _read_optional_file(path)
    _validate_store_path(binding)
    _validate_managed_path(store, path)
    if raw is None:
        return None
    marker = _parse_marker(raw)
    if (
        binding.mode != "cloud"
        or marker.project_id != binding.project_id
        or marker.canonical_bytes() != raw
    ):
        raise GenerationControlCorruptError("The generation authority marker is invalid.")
    if marker.store_format_version != HOSTED_STORE_FORMAT_VERSION:
        raise ClientUpgradeRequiredError(
            "This hosted store format requires another client version."
        )
    return raw
