"""Tests for nauro.sync.remote — mocked boto3 client."""

from unittest.mock import MagicMock, patch

import pytest

from nauro.sync.config import SyncConfig
from nauro.sync.remote import (
    ConflictError,
    check_etag,
    create_client,
    list_remote,
    pull_file,
    push_file,
)


@pytest.fixture
def sync_config():
    return SyncConfig(
        bucket_name="test-bucket",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="secret",
        enabled=True,
    )


@pytest.fixture
def mock_client():
    return MagicMock()


class TestCreateClient:
    @patch("nauro.sync.remote.boto3")
    def test_creates_s3_client(self, mock_boto3, sync_config):
        create_client(sync_config)
        mock_boto3.client.assert_called_once_with(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="AKID",
            aws_secret_access_key="secret",
        )


class TestPushFile:
    def test_push_success(self, mock_client, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello")
        mock_client.put_object.return_value = {"ETag": '"abc123"'}

        etag = push_file(mock_client, "bucket", f, "key/test.md")

        assert etag == '"abc123"'
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "bucket"
        assert call_kwargs["Key"] == "key/test.md"
        assert call_kwargs["Body"] == b"hello"

    def test_push_412_raises_conflict(self, mock_client, tmp_path):
        from botocore.exceptions import ClientError

        f = tmp_path / "test.md"
        f.write_text("hello")
        mock_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )

        with pytest.raises(ConflictError):
            push_file(mock_client, "bucket", f, "key/test.md", expected_etag='"old"')

    def test_push_other_error_raises(self, mock_client, tmp_path):
        from botocore.exceptions import ClientError

        f = tmp_path / "test.md"
        f.write_text("hello")
        mock_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "InternalError"}}, "PutObject"
        )

        with pytest.raises(ClientError):
            push_file(mock_client, "bucket", f, "key/test.md")


class TestPullFile:
    def test_pull_success(self, mock_client, tmp_path):
        body = MagicMock()
        body.read.return_value = b"remote content"
        mock_client.get_object.return_value = {"Body": body, "ETag": '"etag1"'}

        dest = tmp_path / "pulled.md"
        etag = pull_file(mock_client, "bucket", "key/test.md", dest)

        assert etag == '"etag1"'
        assert dest.read_text() == "remote content"

    def test_pull_creates_parent_dirs(self, mock_client, tmp_path):
        body = MagicMock()
        body.read.return_value = b"content"
        mock_client.get_object.return_value = {"Body": body, "ETag": '"e"'}

        dest = tmp_path / "sub" / "dir" / "file.md"
        pull_file(mock_client, "bucket", "key", dest)
        assert dest.exists()


class TestCheckEtag:
    def test_exists(self, mock_client):
        mock_client.head_object.return_value = {"ETag": '"tag1"'}

        result = check_etag(mock_client, "bucket", "key/test.md")
        assert result == '"tag1"'

    def test_not_found(self, mock_client):
        from botocore.exceptions import ClientError

        mock_client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

        result = check_etag(mock_client, "bucket", "key/missing.md")
        assert result is None

    def test_other_error_raises(self, mock_client):
        from botocore.exceptions import ClientError

        mock_client.head_object.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadObject")

        with pytest.raises(ClientError):
            check_etag(mock_client, "bucket", "key/test.md")


class TestListRemote:
    def test_list_objects(self, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "projects/test/project.md", "ETag": '"e1"', "Size": 100},
                    {"Key": "projects/test/state.md", "ETag": '"e2"', "Size": 200},
                ]
            }
        ]

        results = list_remote(mock_client, "bucket", "projects/test/")
        assert len(results) == 2
        assert results[0]["key"] == "projects/test/project.md"
        assert results[0]["etag"] == '"e1"'
        assert results[1]["key"] == "projects/test/state.md"

    def test_list_empty(self, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]

        results = list_remote(mock_client, "bucket", "projects/test/")
        assert results == []

    def test_list_paginated(self, mock_client):
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "a", "ETag": '"e1"', "Size": 10}]},
            {"Contents": [{"Key": "b", "ETag": '"e2"', "Size": 20}]},
        ]

        results = list_remote(mock_client, "bucket", "prefix/")
        assert len(results) == 2
