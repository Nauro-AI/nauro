"""Explicit authentication lifecycle for the selected reference profile."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt

from nauro.auth import ActiveCredentials
from nauro.sync.decision_profile import RenewalProfile, load_reference_profile
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.reference_credentials import CredentialRecord, CredentialStore, profile_binding
from nauro.sync.reference_oauth import callback_code, exchange

AUTH_ERRORS = (
    ValueError,
    OSError,
    httpx.HTTPError,
    jwt.PyJWTError,
    TypeError,
    KeyError,
    ImportError,
)


class ReferenceAuth:
    def __init__(self, profile: RenewalProfile, client: httpx.Client) -> None:
        self.profile, self.client = profile, client
        self.store = CredentialStore(
            Path(profile.credentials_file), profile_binding(profile), profile.actor_id
        )

    def _record(self, tokens: tuple[str, str, int]) -> CredentialRecord:
        access, refresh, expires = tokens
        return CredentialRecord(
            revision=secrets.token_hex(32),
            binding=self.store.binding,
            state="active",
            user_id=self.profile.actor_id,
            access_token=access,
            refresh_token=refresh,
            expires_at=expires,
        )

    def login(self, present_url: Callable[[str], None]) -> None:
        with self.store.locked():
            before = self.store.read()
            revision = before.revision if before else None
        code, verifier = callback_code(self.profile, present_url)
        tokens = exchange(
            self.profile,
            self.client,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.profile.redirect_uri,
            },
        )
        record = self._record(tokens)
        transport = DecisionReferenceTransport(
            self.profile.endpoint,
            self.profile.project_id,
            self.profile.actor_id,
            self.client,
            lambda: ActiveCredentials(record.user_id, record.access_token),
        )
        transport.initialize()
        transport.propose_decision(project_id=self.profile.project_id, request_mode="discover")
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
        def replacement(record: CredentialRecord) -> CredentialRecord:
            return self._record(
                exchange(
                    self.profile,
                    self.client,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": record.refresh_token,
                    },
                )
            )

        renew_credentials(self.store, replacement)

    def logout(self) -> None:
        with self.store.locked():
            self.store.write(self.store.empty("logged_out"))
            self.store.finish()

    def status(self) -> str:
        with self.store.locked():
            record = self.store.read()
            if self.store.incomplete():
                return "reauthentication_required"
            if record is None or record.state == "logged_out":
                return "logged_out"
            if record.state != "active":
                return "reauthentication_required"
            return "active" if record.expires_at > time.time() else "expired"


def run_reference_auth(action: str, path: Path, present_url: Callable[[str], None]) -> str:
    try:
        profile = load_reference_profile(path)
        if not isinstance(profile, RenewalProfile):
            raise ValueError("Renewal requires a version 2 reference profile")
        with httpx.Client() as client:
            auth = ReferenceAuth(profile, client)
            if action == "login":
                auth.login(present_url)
            elif action == "refresh":
                auth.refresh()
            elif action == "logout":
                auth.logout()
            elif action == "status":
                return auth.status()
            else:
                raise ValueError("Unsupported reference authentication action")
        return "Reference credentials updated. No decision request was submitted."
    except AUTH_ERRORS:
        raise ValueError(
            "Reference authentication failed; inspect profile auth status or log in again"
        ) from None


def renew_credentials(
    store: CredentialStore, replacement: Callable[[CredentialRecord], CredentialRecord]
) -> None:
    with store.locked():
        record = store.read()
        if (
            store.incomplete()
            or record is None
            or record.state != "active"
            or not record.refresh_token
        ):
            raise ValueError("Reference login required")
        pending = store.empty("renewal_in_progress")
        store.begin()
        store.write(pending)
        try:
            store.write(replacement(record))
            store.finish()
        except AUTH_ERRORS:
            store.begin()
            store.write(pending)
            raise ValueError("Renewal incomplete; reference login required") from None
