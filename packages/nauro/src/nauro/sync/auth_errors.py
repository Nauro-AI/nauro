"""Safe authentication diagnostics without provider or credential data."""

from __future__ import annotations

import errno

import httpx
import jwt

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
}


class AuthenticationError(ValueError):
    def __init__(self, code: str) -> None:
        self.reason, recovery = _MESSAGES[code]
        super().__init__(f"{self.reason} {recovery}")


class ExchangeNotSentError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("exchange_not_sent")


class RenewalRequiredError(ValueError):
    def __init__(self, cause: Exception) -> None:
        super().__init__(
            f"{auth_error_message(cause, recovery=False)} Renewal incomplete; login required. "
            "Run 'nauro auth login' again."
        )


def auth_error_message(exc: Exception, *, recovery: bool = True) -> str:
    if isinstance(exc, AuthenticationError):
        return str(exc) if recovery else exc.reason
    if isinstance(exc, RenewalRequiredError):
        return str(exc) if recovery else "Credential renewal did not complete."
    if isinstance(exc, PermissionError):
        reason = "Local authentication storage or callback access was denied."
        return reason + (" Check filesystem and sandbox permissions." if recovery else "")
    if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
        reason = "The login callback port is in use."
        return reason + (" Close the other login attempt and try again." if recovery else "")
    messages = (
        (httpx.TimeoutException, "The authentication request timed out."),
        (httpx.HTTPError, "The authentication connection failed."),
        (jwt.PyJWTError, "The returned access token could not be verified."),
    )
    fallback = (
        "Check the project connection and local credentials, or run 'nauro auth login' again."
        if recovery
        else "Authentication failed."
    )
    return next((message for kind, message in messages if isinstance(exc, kind)), fallback)
