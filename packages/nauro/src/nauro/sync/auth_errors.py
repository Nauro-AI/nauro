"""Safe authentication diagnostics without provider or credential data."""

from __future__ import annotations

import errno

import httpx
import jwt

REQUIRED_TOKEN_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "azp", "scope")

_MESSAGES = {
    "exchange_not_sent": (
        "Could not connect to the token endpoint.",
        "Check your connection and retry.",
    ),
    "login_timeout": ("Login timed out.", "Retry the command that started this login."),
    "login_refused": ("Login was refused.", "Retry the command that started this login."),
    "request_rejected": (
        "The authentication service rejected the request.",
        "Check your login settings.",
    ),
    "rate_limited": (
        "The authentication service rate limit was reached.",
        "Wait before trying again.",
    ),
    "service_unavailable": ("The authentication service is unavailable.", "Try again later."),
    "invalid_tokens": (
        "The token response is incomplete or invalid.",
        "Check the OAuth client settings.",
    ),
    "login_required": ("Login required.", "Run 'nauro auth login' for this project."),
    "token_identity": (
        "The token identity does not match this connection.",
        "Check the OAuth client settings.",
    ),
    "token_scopes": (
        "The token is missing required permissions.",
        "Check the OAuth client permissions.",
    ),
    "signing_key": (
        "The access token signing key does not match a supported issuer key.",
        "Check the OAuth issuer settings.",
    ),
    "token_expiry": (
        "The access token expiry claim is not an integer timestamp.",
        "Run 'nauro auth login' again.",
    ),
}


class AuthenticationError(ValueError):
    def __init__(self, code: str) -> None:
        self.reason, recovery = _MESSAGES[code]
        super().__init__(f"{self.reason} {recovery}")


class ExchangeNotSentError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("exchange_not_sent")


class InvalidExpiryClaimError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("token_expiry")


class RenewalRequiredError(ValueError):
    def __init__(self, cause: Exception) -> None:
        super().__init__(
            f"{auth_error_message(cause, recovery=False)} Renewal incomplete; login required. "
            "Run 'nauro auth login' again."
        )


class VerificationRequiredError(ValueError):
    def __init__(self, cause: Exception) -> None:
        recovery = (
            "Run 'nauro auth refresh' to retry verification without another token exchange. "
            "If verification keeps failing, run 'nauro auth login'."
        )
        if isinstance(cause, jwt.ExpiredSignatureError):
            recovery = (
                "Run 'nauro auth login'. "
                "If this machine's clock is incorrect, correct it before signing in."
            )
        elif isinstance(cause, InvalidExpiryClaimError):
            recovery = "Run 'nauro auth login' again."
        super().__init__(
            f"{auth_error_message(cause, recovery=False)} Access remains blocked. {recovery}"
        )


def auth_error_message(exc: Exception, *, recovery: bool = True) -> str:
    if isinstance(exc, AuthenticationError):
        return str(exc) if recovery else exc.reason
    if isinstance(exc, RenewalRequiredError):
        return str(exc) if recovery else "Credential renewal did not complete."
    if isinstance(exc, VerificationRequiredError):
        return str(exc) if recovery else "Received credentials need verification."
    if isinstance(exc, PermissionError):
        reason = "Local authentication storage or callback access was denied."
        return reason + (" Check filesystem and sandbox permissions." if recovery else "")
    if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
        reason = "The login callback port is in use."
        return reason + (" Close the other login attempt and try again." if recovery else "")
    missing_claim = (
        f"The access token is missing the required claim '{exc.claim}'."
        if isinstance(exc, jwt.MissingRequiredClaimError) and exc.claim in REQUIRED_TOKEN_CLAIMS
        else "The access token is missing a required claim."
    )
    messages = (
        (httpx.TimeoutException, "The authentication request timed out."),
        (httpx.HTTPError, "The authentication connection failed."),
        (jwt.ExpiredSignatureError, "The returned access token has expired."),
        (jwt.ImmatureSignatureError, "The token timestamp is ahead of this machine's clock."),
        (jwt.InvalidSignatureError, "The access token signature is invalid."),
        (jwt.InvalidIssuerError, "The access token issuer does not match this connection."),
        (jwt.InvalidAudienceError, "The access token audience does not match this connection."),
        (jwt.MissingRequiredClaimError, missing_claim),
        (jwt.InvalidIssuedAtError, "The access token has an invalid issue timestamp."),
        (jwt.DecodeError, "The access token format or claim types are invalid."),
        (
            jwt.PyJWTError,
            f"The returned access token could not be verified ({type(exc).__name__}).",
        ),
    )
    fallback = (
        "Check the project connection and local credentials, or run 'nauro auth login' again."
        if recovery
        else "Authentication failed."
    )
    return next((message for kind, message in messages if isinstance(exc, kind)), fallback)
