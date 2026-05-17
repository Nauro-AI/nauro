"""S3 remote client for cloud sync.

Two transports coexist here during the cutover:

* The legacy direct-S3 helpers (``create_client``, ``push_file``, ``pull_file``,
  ``list_remote``) use the user's static IAM credentials.
* The new presign helpers (``fetch_manifest``, ``request_presigned_urls``,
  ``put_via_presigned_url``, ``get_via_presigned_url``) talk to the remote MCP
  server with an Auth0 bearer token. The server mints short-lived presigned
  URLs, then the CLI does the bulk transfer directly against S3.

The legacy path stays callable until PR C removes it (2026-08-15).
"""

import logging
import os
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.exceptions import ClientError

from nauro.cli.commands.auth import (
    DEFAULT_API_URL,
    AuthRefreshError,
    with_token_refresh,
)
from nauro.store.config import load_config
from nauro.sync.config import SyncConfig

logger = logging.getLogger("nauro.sync")

_DEFAULT_API_TIMEOUT = 15.0
_DEFAULT_TRANSFER_TIMEOUT = 60.0


class PresignError(Exception):
    """Raised for unrecoverable failures hitting the manifest/presign endpoints."""


def _resolve_api_url() -> str:
    """Resolve the remote MCP server base URL.

    Mirrors ``nauro.sync.cloud_projects._resolve_api_url`` — env var first,
    then ``api_url`` in user config, then the public default.
    """
    env_url = os.environ.get("NAURO_API_URL")
    if env_url:
        return env_url
    config_url = str(load_config().get("api_url") or "")
    return (config_url or DEFAULT_API_URL).rstrip("/")


class ConflictError(Exception):
    """Raised when a conditional PUT fails due to ETag mismatch (412)."""


def create_client(config: SyncConfig):
    """Create an S3 client for the configured region."""
    return boto3.client(
        "s3",
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
    )


def push_file(
    client, bucket: str, local_path: Path, remote_key: str, expected_etag: str | None = None
) -> str | None:
    """PUT object to S3. Returns new ETag, or raises ConflictError on 412.

    If expected_etag is provided, uses If-Match for optimistic concurrency.
    """
    data = local_path.read_bytes()
    kwargs: dict = {"Bucket": bucket, "Key": remote_key, "Body": data}
    if expected_etag:
        kwargs["IfMatch"] = expected_etag

    try:
        response = client.put_object(**kwargs)
        return response.get("ETag", "")  # type: ignore[no-any-return]
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code in ("PreconditionFailed", "412"):
            raise ConflictError(f"Remote changed for {remote_key}") from e
        raise


def pull_file(client, bucket: str, remote_key: str, local_path: Path) -> str:
    """GET object from S3, write to local_path. Return the ETag."""
    response = client.get_object(Bucket=bucket, Key=remote_key)
    content = response["Body"].read()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)
    return response.get("ETag", "")  # type: ignore[no-any-return]


def check_etag(client, bucket: str, remote_key: str) -> str | None:
    """HEAD request. Return ETag if exists, None if 404."""
    try:
        response = client.head_object(Bucket=bucket, Key=remote_key)
        return response.get("ETag", "")  # type: ignore[no-any-return]
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code in ("404", "NoSuchKey"):
            return None
        raise


def list_remote(client, bucket: str, prefix: str) -> list[dict]:
    """LIST objects under prefix. Return list of {key, etag, last_modified, size}."""
    results = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            results.append(
                {
                    "key": obj["Key"],
                    "etag": obj.get("ETag", ""),
                    "last_modified": obj.get("LastModified"),
                    "size": obj.get("Size", 0),
                }
            )
    return results


# Presign transport — Auth0 bearer + remote MCP server.
# The server batches up to 200 ops per /sync/presign call (see mcp-server
# _PRESIGN_OPS_BATCH_LIMIT); the CLI chunks larger diffs to match.
_PRESIGN_BATCH_LIMIT = 200


def fetch_manifest(project_id: str) -> list[dict]:
    """Return every server-side file entry for ``project_id``.

    Each entry mirrors the server payload: ``{"path", "etag", "size",
    "last_modified"}`` where ``path`` is project-root relative.
    Pagination is collapsed for the caller — the cursor stays internal.
    """
    api_url = _resolve_api_url()
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

    api_url = _resolve_api_url()
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
    "AuthRefreshError",
    "ConflictError",
    "PresignError",
    "check_etag",
    "create_client",
    "fetch_manifest",
    "fetch_via_presigned_url",
    "get_via_presigned_url",
    "list_remote",
    "pull_file",
    "push_file",
    "put_via_presigned_url",
    "request_presigned_urls",
]
