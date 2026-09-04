"""Memory-only acquisition of one verified hosted generation projection."""

from __future__ import annotations

import base64
from functools import partial

import httpx
import pydantic as pyd
from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.auth import with_token_refresh
from nauro.store.generation_authority import (
    ClientUpgradeRequiredError,
    GenerationAuthorityError,
    RefreshRequiredError,
    ReplicaActorMismatchError,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    _parse_manifest,
    _strict_json_preflight,
    verify_generation_projection,
)
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.remote import (
    _DEFAULT_API_TIMEOUT,
    _DEFAULT_TRANSFER_TIMEOUT,
    PRESIGN_BATCH_LIMIT,
    TransferBoundaryError,
    TransferOperation,
    TransferSession,
    _get,
    _invalid_response,
    _post,
    _status_error,
    canonical_origin,
    classify_status,
    operation_session,
    resolve_api_url,
)
from nauro.sync.transfer import download_with_retry

_PROJECTION_ROUTE = "/generations/projection"
_PRESIGN_ROUTE = "/generations/presign"
_MAX_PROJECTION_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_PROJECTION_BYTES = 64 * 1024 * 1024
_MAX_ACQUISITION_ATTEMPTS = 3
_STRICT = pyd.ConfigDict(extra="forbid", frozen=True, strict=True)
_PARSE_FAILURES = (pyd.ValidationError, ValueError, TypeError, RecursionError)
_NO_ACTOR = "Generation acquisition requires the active account's user id."
_OTHER_ACCOUNT = "The generation projection was issued for another account."
_SUPERSEDED = "The committed generation advanced during acquisition."
_UPGRADE_REQUIRED = "This hosted store format requires another client version."


class GenerationAcquisitionError(GenerationAuthorityError):
    code = "generation_acquisition_failed"


class GenerationSupersededError(GenerationAuthorityError):
    code = "generation_superseded"


class LegacyStoreAuthorityError(GenerationAuthorityError):
    code = "legacy_store_authority"


class _ProjectionResponse(pyd.BaseModel):
    model_config = _STRICT
    projection: GenerationProjectionIdentity
    manifest_base64: pyd.StrictStr


class _PresignEntry(pyd.BaseModel):
    model_config = _STRICT
    path: pyd.StrictStr
    url: pyd.StrictStr


class _PresignResponse(pyd.BaseModel):
    model_config = _STRICT
    projection: GenerationProjectionIdentity
    urls: tuple[_PresignEntry, ...]
    expires_at: pyd.StrictStr


def _mapped_refusal(response: httpx.Response) -> GenerationAuthorityError | None:
    if response.status_code != 409:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if error == "client_upgrade_required":
        return ClientUpgradeRequiredError(_UPGRADE_REQUIRED)
    if error == "legacy_store_authority":
        return LegacyStoreAuthorityError("The project is not on generation authority.")
    if error == "generation_superseded":
        return GenerationSupersededError(_SUPERSEDED)
    return None


def _api_response(
    session: TransferSession, endpoint: str, operation: TransferOperation, **request: object
) -> httpx.Response:
    send = _get if operation is TransferOperation.MANIFEST else _post

    def call(token: str) -> httpx.Response:
        response: httpx.Response = send(
            session,
            endpoint,
            operation,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_DEFAULT_API_TIMEOUT,
            **request,
        )
        return response

    response = with_token_refresh(call, client=session.client)
    if response.status_code == 200:
        return response
    # Typed refusals are mapped ahead of the status classifier because a
    # permanent MANIFEST or PRESIGN status trips the origin and would block
    # the restart that a superseded generation needs.
    refusal = _mapped_refusal(response)
    if refusal is not None:
        raise refusal
    raise _status_error(session, endpoint, operation, response.status_code)


def _fetch_projection(
    session: TransferSession, api_url: str, binding: ResolvedProjectBinding, user_id: str
) -> tuple[GenerationProjectionTarget, bytes]:
    response = _api_response(
        session,
        api_url + _PROJECTION_ROUTE,
        TransferOperation.MANIFEST,
        params={"project_id": binding.project_id},
    )
    if len(response.content) > _MAX_PROJECTION_RESPONSE_BYTES:
        raise GenerationAcquisitionError("The generation projection response exceeds the size cap.")
    try:
        _strict_json_preflight(response.content)
        parsed = _ProjectionResponse.model_validate_json(response.content)
    except _PARSE_FAILURES:
        parsed = None
    if parsed is None:
        raise GenerationAcquisitionError("The generation projection response is malformed.")
    target = GenerationProjectionTarget(binding, parsed.projection)
    if target.identity.installed_for_user_id != user_id:
        raise ReplicaActorMismatchError(_OTHER_ACCOUNT)
    carrier = parsed.manifest_base64
    try:
        envelope = base64.b64decode(carrier, validate=True)
    except ValueError:
        envelope = None
    if envelope is None or base64.b64encode(envelope) != carrier.encode("ascii"):
        raise GenerationAcquisitionError("The generation manifest carrier is not canonical base64.")
    return target, envelope


def _compare_identity(
    first: GenerationProjectionIdentity, page: GenerationProjectionIdentity
) -> None:
    if page.generation_id != first.generation_id:
        raise GenerationSupersededError(_SUPERSEDED)
    if page.installed_for_user_id != first.installed_for_user_id:
        raise ReplicaActorMismatchError(_OTHER_ACCOUNT)
    if page.model_dump() != first.model_dump():
        raise RefreshRequiredError("The authorization view changed during acquisition.")


def _presign(
    session: TransferSession,
    api_url: str,
    identity: GenerationProjectionIdentity,
    paths: tuple[str, ...],
) -> dict[str, str]:
    endpoint = api_url + _PRESIGN_ROUTE
    response = _api_response(
        session,
        endpoint,
        TransferOperation.PRESIGN,
        json={
            "project_id": identity.project_id,
            "generation_id": identity.generation_id,
            "paths": list(paths),
        },
    )
    try:
        _strict_json_preflight(response.content)
        page = _PresignResponse.model_validate_json(response.content)
    except _PARSE_FAILURES:
        page = None
    if page is None:
        raise _invalid_response(session, endpoint, TransferOperation.PRESIGN, response.status_code)
    _compare_identity(identity, page.projection)
    returned = tuple(entry.path for entry in page.urls)
    if len(set(returned)) != len(returned) or set(returned) != set(paths):
        raise GenerationAcquisitionError(
            "The presign response does not match the requested artifact set."
        )
    if any(not entry.url.startswith("https://") for entry in page.urls):
        raise GenerationAcquisitionError("Presigned URLs must use https.")
    return {entry.path: entry.url for entry in page.urls}


class _PageUrls:
    def __init__(
        self,
        session: TransferSession,
        api_url: str,
        identity: GenerationProjectionIdentity,
        paths: tuple[str, ...],
    ) -> None:
        self._session = session
        self._api_url = api_url
        self._identity = identity
        self._outstanding = list(paths)
        self._urls = _presign(session, api_url, identity, paths)

    def url_for(self, path: str) -> str:
        return self._urls[path]

    def remint(self) -> None:
        outstanding = tuple(self._outstanding)
        self._urls = _presign(self._session, self._api_url, self._identity, outstanding)

    def done(self, path: str) -> None:
        self._outstanding.remove(path)


def _fetch_artifact(session: TransferSession, path: str, url: str) -> bytes:
    session.guard(url, TransferOperation.GET)
    oversize = f"The generation artifact exceeds the size cap: {path}."
    chunks: list[bytes] = []
    error: TransferBoundaryError | None = None
    try:
        with session.client.stream("GET", url, timeout=_DEFAULT_TRANSFER_TIMEOUT) as response:
            if response.status_code != 200:
                fault = classify_status(response.status_code, operation=TransferOperation.GET)
                raise TransferBoundaryError(
                    operation=TransferOperation.GET,
                    origin=canonical_origin(url),
                    kind=fault.kind,
                    retry=fault.retry,
                    write_outcome=fault.write_outcome,
                    status=response.status_code,
                )
            declared = response.headers.get("Content-Length", "")
            if declared.isascii() and declared.isdigit() and int(declared) > _MAX_ARTIFACT_BYTES:
                raise GenerationAcquisitionError(oversize)
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > _MAX_ARTIFACT_BYTES:
                    raise GenerationAcquisitionError(oversize)
                chunks.append(chunk)
    except (GenerationAuthorityError, TransferBoundaryError):
        raise
    except Exception as exc:
        error = session.boundary_error(url, TransferOperation.GET, exc)
    if error is not None:
        raise error from None
    return b"".join(chunks)


def _acquire_once(
    session: TransferSession, api_url: str, binding: ResolvedProjectBinding, user_id: str
) -> VerifiedGenerationProjection:
    target, envelope = _fetch_projection(session, api_url, binding, user_id)
    manifest = _parse_manifest(target, envelope)
    paths = tuple(manifest.artifacts)
    bodies: list[tuple[str, bytes]] = []
    total = 0
    for start in range(0, len(paths), PRESIGN_BATCH_LIMIT):
        chunk = paths[start : start + PRESIGN_BATCH_LIMIT]
        page = _PageUrls(session, api_url, target.identity, chunk)
        for path in chunk:
            body = download_with_retry(path, page, partial(_fetch_artifact, session, path))
            if total + len(body) > _MAX_PROJECTION_BYTES:
                raise GenerationAcquisitionError(
                    "The generation projection exceeds the total size cap."
                )
            total += len(body)
            bodies.append((path, body))
            page.done(path)
    return verify_generation_projection(target, manifest_json=envelope, artifacts=tuple(bodies))


def acquire_generation_projection(
    binding: ResolvedProjectBinding,
    *,
    active_user_id: str | None,
    session: TransferSession | None = None,
) -> VerifiedGenerationProjection:
    if binding.mode != "cloud":
        raise GenerationAcquisitionError("Generation acquisition requires a cloud project binding.")
    if type(active_user_id) is not str:
        raise ReplicaActorMismatchError(_NO_ACTOR)
    try:
        user_id = validate_identifier(IdentifierKind.ulid, active_user_id, field="active_user_id")
    except ValueError as exc:
        raise ReplicaActorMismatchError(_NO_ACTOR) from exc
    with operation_session(session) as active:
        api_url = resolve_api_url()
        attempts = 0
        while True:
            attempts += 1
            try:
                return _acquire_once(active, api_url, binding, user_id)
            except GenerationSupersededError:
                if attempts >= _MAX_ACQUISITION_ATTEMPTS:
                    raise


__all__ = [
    "GenerationAcquisitionError",
    "GenerationSupersededError",
    "LegacyStoreAuthorityError",
    "acquire_generation_projection",
]
