"""Operator-selected profile and private credentials for reference delivery."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from nauro_core.identifiers import IdentifierKind, validate_identifier
from pydantic import BaseModel, ConfigDict, model_validator

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


class RenewalProfile(ReferenceProfile):
    version: Literal[2]  # type: ignore[assignment]
    issuer: str
    client_id: str
    audience: str
    expected_subject: str
    redirect_uri: str

    @model_validator(mode="after")
    def validate_endpoints(self) -> RenewalProfile:
        for value in (self.project_id, self.actor_id):
            validate_identifier(IdentifierKind.ulid, value, field="reference_scope")
        issuer, endpoint, redirect = map(urlsplit, (self.issuer, self.endpoint, self.redirect_uri))
        for url in (issuer, endpoint):
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("HTTPS authentication endpoints required")
        if endpoint.path != "/mcp":
            raise ValueError("MCP endpoint path required")
        if issuer.path != "/" or not self.issuer.endswith("/"):
            raise ValueError("Issuer must be an HTTPS origin with a trailing slash")
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or not redirect.port
            or redirect.path != "/callback"
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError("Explicit IPv4 loopback callback required")
        if not all((self.client_id, self.audience, self.expected_subject)):
            raise ValueError("Authentication pins are required")
        return self


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
    raw = _private_json(path)
    model = (
        RenewalProfile if isinstance(raw, dict) and raw.get("version") == 2 else ReferenceProfile
    )
    profile = model.model_validate(raw)
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
        lambda: profile_credentials(profile),
    )


def profile_credentials(profile: ReferenceProfile) -> ActiveCredentials:
    if not isinstance(profile, RenewalProfile):
        return read_reference_credentials(Path(profile.credentials_file))
    from nauro.sync.reference_credentials import CredentialStore, profile_binding

    try:
        store = CredentialStore(
            Path(profile.credentials_file), profile_binding(profile), profile.actor_id
        )
        with store.locked():
            record = store.read()
            if (
                store.incomplete()
                or record is None
                or record.state != "active"
                or not record.access_token
            ):
                raise ValueError("Reference login required")
            return ActiveCredentials(record.user_id, record.access_token)
    except (ValueError, OSError, ImportError):
        raise ValueError("Reference credentials unavailable; use profile auth status") from None
