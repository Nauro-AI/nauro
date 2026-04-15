"""S3 remote client for cloud sync."""

import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from nauro.sync.config import SyncConfig

logger = logging.getLogger("nauro.sync")


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
