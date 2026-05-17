"""Remote sync client — manifest + presign endpoints against the MCP server.

The CLI obtains short-lived presigned URLs from the server and does the
bulk transfer directly against S3. Auth is the Auth0 bearer; the
``with_token_refresh`` wrapper handles 401-and-retry transparently.
"""

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from nauro.cli.commands.auth import (
    DEFAULT_API_URL,
    with_token_refresh,
)
from nauro.store.config import load_config

logger = logging.getLogger("nauro.sync")

_DEFAULT_API_TIMEOUT = 15.0
_DEFAULT_TRANSFER_TIMEOUT = 60.0


class PresignError(Exception):
    """Raised for unrecoverable failures hitting the manifest/presign endpoints."""


def resolve_api_url() -> str:
    """Resolve the remote MCP server base URL.

    Precedence: ``NAURO_API_URL`` env var, then ``api_url`` in user config,
    then the public default. Trailing slash stripped on every branch so
    callers can append ``/path`` without doubling the separator.
    """
    env_url = os.environ.get("NAURO_API_URL") or ""
    config_url = str(load_config().get("api_url") or "")
    return (env_url or config_url or DEFAULT_API_URL).rstrip("/")


# The server batches up to 200 ops per /sync/presign call (see mcp-server
# _PRESIGN_OPS_BATCH_LIMIT); the CLI chunks larger diffs to match.
_PRESIGN_BATCH_LIMIT = 200


def fetch_manifest(project_id: str) -> list[dict]:
    """Return every server-side file entry for ``project_id``.

    Each entry mirrors the server payload: ``{"path", "etag", "size",
    "last_modified"}`` where ``path`` is project-root relative.
    Pagination is collapsed for the caller — the cursor stays internal.
    """
    api_url = resolve_api_url()
    files: list[dict] = []
    cursor: str | None = None

    while True:
        params: dict[str, str] = {"project_id": project_id}
        if cursor:
            params["cursor"] = cursor

        def _call(token: str, params=params) -> httpx.Response:
            return httpx.get(
                f"{api_url}/sync/manifest",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_DEFAULT_API_TIMEOUT,
            )

        response = with_token_refresh(_call)
        if response.status_code != 200:
            raise PresignError(
                f"GET /sync/manifest failed ({response.status_code}): {response.text}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PresignError(f"Manifest response was not JSON: {exc}") from exc

        page_files = body.get("files") if isinstance(body, dict) else None
        if not isinstance(page_files, list):
            raise PresignError(f"Unexpected manifest shape: {body!r}")
        files.extend(page_files)

        next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
        if not next_cursor:
            return files
        cursor = str(next_cursor)


def request_presigned_urls(
    project_id: str, operations: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Mint presigned URLs for a batch of GET/PUT operations.

    ``operations`` items are ``{"verb": "GET"|"PUT", "path": "..."}``. The
    server caps each request at 200 ops; this helper chunks transparently
    so the caller can pass an arbitrary diff.
    """
    if not operations:
        return []

    api_url = resolve_api_url()
    all_urls: list[dict[str, Any]] = []

    for start in range(0, len(operations), _PRESIGN_BATCH_LIMIT):
        chunk = operations[start : start + _PRESIGN_BATCH_LIMIT]

        def _call(token: str, chunk=chunk) -> httpx.Response:
            return httpx.post(
                f"{api_url}/sync/presign",
                json={"project_id": project_id, "operations": chunk},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_DEFAULT_API_TIMEOUT,
            )

        response = with_token_refresh(_call)
        if response.status_code != 200:
            raise PresignError(
                f"POST /sync/presign failed ({response.status_code}): {response.text}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PresignError(f"Presign response was not JSON: {exc}") from exc

        urls = body.get("urls") if isinstance(body, dict) else None
        if not isinstance(urls, list):
            raise PresignError(f"Unexpected presign shape: {body!r}")
        all_urls.extend(urls)

    return all_urls


def fetch_via_presigned_url(url: str) -> bytes:
    """GET ``url`` and return the body bytes (no local write)."""
    response = httpx.get(url, timeout=_DEFAULT_TRANSFER_TIMEOUT)
    if response.status_code != 200:
        raise PresignError(f"Presigned GET failed ({response.status_code})")
    return response.content


def get_via_presigned_url(url: str, local_path: Path) -> bytes:
    """GET ``url`` and write the body to ``local_path``. Returns the bytes."""
    content = fetch_via_presigned_url(url)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)
    return content


def put_via_presigned_url(url: str, local_path: Path) -> str:
    """PUT the bytes at ``local_path`` to ``url``. Returns the new ETag."""
    data = local_path.read_bytes()
    response = httpx.put(url, content=data, timeout=_DEFAULT_TRANSFER_TIMEOUT)
    if response.status_code not in (200, 204):
        raise PresignError(f"Presigned PUT failed ({response.status_code}) for {local_path}")
    return response.headers.get("ETag", "")


__all__ = [
    "PresignError",
    "fetch_manifest",
    "fetch_via_presigned_url",
    "get_via_presigned_url",
    "put_via_presigned_url",
    "request_presigned_urls",
    "resolve_api_url",
]
