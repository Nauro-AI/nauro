"""Operator-selected profile and private credentials for reference delivery."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from nauro.auth import ActiveCredentials
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.decision_reference_contract import _json


class ReferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    endpoint: str
    project_id: str
    actor_id: str
    credentials_file: str


class ReferenceCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    access_token: str


def _private_json(path: Path) -> Any:
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "getuid")):
        raise ValueError("Private reference files are unsupported on this platform")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Use an owner-only regular file")
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("File exceeds size limit")
        return _json(raw)


def read_reference_credentials(path: Path) -> ActiveCredentials:
    try:
        record = ReferenceCredentials.model_validate(_private_json(path))
        if not record.user_id or not record.access_token:
            raise ValueError("Missing credentials")
        return ActiveCredentials(record.user_id, record.access_token)
    except (ValueError, OSError):
        raise ValueError("Reference credentials unavailable") from None


def load_reference_profile(path: Path) -> ReferenceProfile:
    profile = ReferenceProfile.model_validate(_private_json(path))
    if not Path(profile.credentials_file).is_absolute():
        raise ValueError("Credentials path must be absolute")
    return profile


def profile_transport(
    profile: ReferenceProfile, client: httpx.Client
) -> DecisionReferenceTransport:
    return DecisionReferenceTransport(
        profile.endpoint,
        profile.project_id,
        profile.actor_id,
        client,
        lambda: read_reference_credentials(Path(profile.credentials_file)),
    )
