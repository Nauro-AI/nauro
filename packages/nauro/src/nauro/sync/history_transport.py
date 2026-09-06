"""Explicit authenticated history reads with bounded response verification."""

from __future__ import annotations

import json

import httpx

from nauro.auth import read_active_credentials
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_projection import GenerationProjectionTarget, _target_parts
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.history_contract import MAX_RESPONSE_BYTES, HistoryResponse


class HistoryTransportError(GenerationAuthorityError):
    code = "generation_history_unavailable"


def _origin(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise HistoryTransportError("History requires a trusted HTTPS origin.") from exc
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise HistoryTransportError("History requires a trusted HTTPS origin.")
    return str(url).rstrip("/")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON key.")
    return result


def _frame(response: HistoryResponse) -> str:
    baseline = response.baseline
    frame = (
        f"Baseline: {baseline.generation_id}. Committed: {baseline.committed_at}.\n"
        if baseline
        else "Baseline: none (committed root).\n"
    )
    head = response.read_authority
    return frame + (
        f"Generation: {head.generation_id}. Committed: {head.committed_at}.\n"
        "Authorization checked for this read."
    )


def verify_history_response(
    raw: bytes, target: GenerationProjectionTarget, days: int | None
) -> HistoryResponse:
    _, identity = _target_parts(target)
    try:
        if days is not None and type(days) is not int:
            raise ValueError("History days must be an integer.")
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("History response exceeds the byte limit.")
        response = HistoryResponse.model_validate(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        )
        if not response.matches(identity, days):
            raise ValueError("History response binding differs.")
        if response.text != response.diff + "\n\n" + _frame(response):
            raise ValueError("History response framing differs.")
        if (
            response.baseline is not None
            and response.baseline.generation_id == identity.generation_id
            and response.baseline.committed_at != identity.committed_at
        ):
            raise ValueError("History head and baseline disagree.")
        return response
    except (ValueError, TypeError, RecursionError) as exc:
        raise HistoryTransportError("The server history response did not verify.") from exc


class HttpHistoryTransport:
    """Use a caller-owned HTTP client without redirects or automatic resends."""

    def __init__(self, base_url: str, client: httpx.Client) -> None:
        self._origin = _origin(base_url)
        self._client = client

    def require_binding(self, binding: ResolvedProjectBinding, api_url: str | None = None) -> None:
        if (
            binding.mode != "cloud"
            or binding.server_url is None
            or _origin(binding.server_url) != self._origin
        ):
            raise HistoryTransportError("History server differs from the project binding.")
        if api_url is not None and _origin(api_url) != self._origin:
            raise HistoryTransportError("Projection server differs from the history server.")

    def fetch(self, target: GenerationProjectionTarget, days: int | None = None) -> HistoryResponse:
        binding, identity = _target_parts(target)
        self.require_binding(binding)
        if days is not None and type(days) is not int:
            raise HistoryTransportError("History days must be an integer.")
        credentials = read_active_credentials()
        if credentials.user_id != identity.installed_for_user_id:
            raise HistoryTransportError("The active account differs from the installed projection.")
        body = {
            "version": 1,
            "project_id": identity.project_id,
            "expected_user_id": identity.installed_for_user_id,
            "expected_generation_id": identity.generation_id,
            "expected_manifest_digest": identity.manifest_digest,
            "expected_projection_scope_id": identity.projection_scope_id,
            "days": days,
        }
        try:
            with self._client.stream(
                "POST",
                self._origin + "/generations/history",
                json=body,
                headers={"Authorization": f"Bearer {credentials.access_token}"},
                timeout=25,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise HistoryTransportError("Authorized history is unavailable.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise HistoryTransportError("The history response exceeds the byte limit.")
                    raw.extend(chunk)
        except (httpx.HTTPError, ValueError) as exc:
            raise HistoryTransportError("Authorized history is unavailable.") from exc
        result = verify_history_response(bytes(raw), target, days)
        if read_active_credentials().user_id != identity.installed_for_user_id:
            raise HistoryTransportError("The active account changed during the history read.")
        return result
