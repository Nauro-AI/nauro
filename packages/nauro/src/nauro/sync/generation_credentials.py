"""Normal account credentials for projects on generation authority."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from nauro_core.identifiers import IdentifierKind, validate_identifier
from pydantic import BaseModel, ConfigDict, model_validator

from nauro.auth import ActiveCredentials
from nauro.store.home import nauro_home
from nauro.sync.decision_profile import _private_json
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.reference_auth import AUTH_ERRORS, renew_credentials
from nauro.sync.reference_credentials import CredentialRecord, CredentialStore
from nauro.sync.reference_oauth import (
    callback_code,
    exchange_tokens,
    response_json,
    verified_claims,
)


class GenerationConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    endpoint: str
    issuer: str
    client_id: str
    audience: str
    redirect_uri: str

    @model_validator(mode="after")
    def validate_urls(self) -> GenerationConnection:
        for value, path in ((self.endpoint, "/mcp"), (self.issuer, "/")):
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path != path
            ):
                raise ValueError("Trusted HTTPS connection required")
        redirect = urlsplit(self.redirect_uri)
        if (
            redirect.scheme != "http"
            or redirect.hostname not in {"127.0.0.1", "localhost"}
            or not redirect.port
            or redirect.path != "/callback"
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError("Loopback callback required")
        if not self.client_id or not self.audience:
            raise ValueError("Authentication pins required")
        return self

    def binding(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    def store(self) -> AccountStore:
        return AccountStore(
            nauro_home() / f"generation-credentials-{self.binding()}.json", self.binding()
        )


class AccountRecord(CredentialRecord):
    version: Literal[3] = 3  # type: ignore[assignment]
    subject: str = ""

    @model_validator(mode="after")
    def validate_identity(self) -> AccountRecord:
        if self.user_id:
            validate_identifier(IdentifierKind.ulid, self.user_id, field="user_id")
        if self.state == "active" and not all(
            (self.user_id, self.subject, self.access_token, self.refresh_token)
        ):
            raise ValueError("Verified account identity required")
        return self


class AccountStore(CredentialStore):
    def __init__(self, path: Path, binding: str) -> None:
        super().__init__(path, binding, "")

    def read(self) -> AccountRecord | None:
        try:
            if self.path.lstat().st_nlink != 1:
                raise ValueError("Credential hard links are refused")
            record = AccountRecord.model_validate(_private_json(self.path))
        except FileNotFoundError:
            return None
        if record.binding != self.binding:
            raise ValueError("Credentials differ from the selected connection")
        return record

    def empty(self, state: Literal["logged_out", "renewal_in_progress"]) -> AccountRecord:
        before = self.read()
        return AccountRecord(
            revision=secrets.token_hex(32),
            binding=self.binding,
            state=state,
            user_id=before.user_id if before else "",
            subject=before.subject if before else "",
        )


def generation_credentials(connection: GenerationConnection, actor: str) -> ActiveCredentials:
    store = connection.store()
    with store.locked():
        record = store.read()
        if (
            store.incomplete()
            or record is None
            or record.state != "active"
            or record.expires_at <= time.time()
            or record.user_id != actor
        ):
            raise ValueError("Generation login or explicit refresh required")
        return ActiveCredentials(record.user_id, record.access_token)


class GenerationAuth:
    def __init__(
        self, connection: GenerationConnection, project: str, client: httpx.Client
    ) -> None:
        validate_identifier(IdentifierKind.ulid, project, field="project")
        self.connection, self.project, self.client = connection, project, client
        self.store = connection.store()

    def _tokens(self, grant: dict[str, str]) -> tuple[str, str, dict]:
        access, refresh = exchange_tokens(self.connection, self.client, grant)
        return access, refresh, verified_claims(self.connection, access, self.client)

    def _record(self, access: str, refresh: str, claims: dict, actor: str) -> AccountRecord:
        return AccountRecord(
            revision=secrets.token_hex(32),
            binding=self.store.binding,
            state="active",
            user_id=actor,
            subject=claims["sub"],
            access_token=access,
            refresh_token=refresh,
            expires_at=claims["exp"],
        )

    def login(self, present_url: Callable[[str], None]) -> None:
        with self.store.locked():
            before = self.store.read()
            revision = before.revision if before else None
        code, verifier = callback_code(self.connection, present_url)
        access, refresh, claims = self._tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.connection.redirect_uri,
            }
        )
        identity = response_json(
            self.client,
            "GET",
            self.connection.endpoint.removesuffix("/mcp") + "/me",
            headers={"Authorization": "Bearer " + access},
        )
        actor = validate_identifier(IdentifierKind.ulid, identity.get("user_id"), field="user_id")
        record = self._record(access, refresh, claims, actor)
        transport = DecisionReferenceTransport(
            self.connection.endpoint,
            self.project,
            actor,
            self.client,
            lambda: ActiveCredentials(actor, access),
        )
        transport.propose_decision(project_id=self.project, request_mode="discover")
        with self.store.locked():
            current = self.store.read()
            if (current.revision if current else None) != revision:
                raise ValueError("Credentials changed during login; login again")
            self.store.begin()
            try:
                self.store.write(record)
                self.store.finish()
            except AUTH_ERRORS:
                self.store.begin()
                raise

    def refresh(self) -> None:
        def replacement(before: CredentialRecord) -> AccountRecord:
            if not isinstance(before, AccountRecord):
                raise ValueError("Generation login required")
            access, refresh, claims = self._tokens(
                {"grant_type": "refresh_token", "refresh_token": before.refresh_token}
            )
            if claims["sub"] != before.subject:
                raise ValueError("Account changed during renewal")
            return self._record(access, refresh, claims, before.user_id)

        renew_credentials(self.store, replacement)

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(self.store.empty("logged_out"))
            self.store.finish()

    def status(self) -> str:
        with self.store.locked():
            record = self.store.read()
            if self.store.incomplete() or (record and record.state == "renewal_in_progress"):
                return "reauthentication_required"
            if record is None or record.state == "logged_out":
                return "logged_out"
            return "active" if record.expires_at > time.time() else "expired"
