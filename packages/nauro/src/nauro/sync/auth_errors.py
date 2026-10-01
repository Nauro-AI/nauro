"""Safe authentication diagnostics without provider or credential data."""

from __future__ import annotations

import errno

import httpx
import jwt

_MESSAGES = {
    "exchange_not_sent": (
        "Could not connect to the token endpoint. Check your connection and retry."
    ),
    "login_timeout": "Login timed out. Run 'nauro auth login' again.",
    "login_refused": "Login was refused. Run 'nauro auth login' to try again.",
    "request_rejected": (
        "The authentication service rejected the request. Check your login settings."
    ),
    "rate_limited": "The authentication service rate limit was reached. Wait before trying again.",
    "service_unavailable": "The authentication service is unavailable. Try again later.",
    "invalid_tokens": (
        "The token response is incomplete or invalid. Check the OAuth client settings."
    ),
    "login_required": "Login required. Run 'nauro auth login' for this project.",
}


class AuthenticationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])


class ExchangeNotSentError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("exchange_not_sent")


class RenewalRequiredError(ValueError):
    def __init__(self, cause: Exception) -> None:
        super().__init__(
            f"{auth_error_message(cause)} Renewal incomplete; login required. "
            "Run 'nauro auth login' again."
        )


def auth_error_message(exc: Exception) -> str:
    if isinstance(exc, (AuthenticationError, RenewalRequiredError)):
        return str(exc)
    if isinstance(exc, PermissionError):
        return (
            "Local authentication storage or callback access was denied. "
            "Check filesystem and sandbox permissions."
        )
    if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
        return "The login callback port is in use. Close the other login attempt and try again."
    if isinstance(exc, httpx.TimeoutException):
        return "The authentication request timed out."
    if isinstance(exc, httpx.HTTPError):
        return "The authentication connection failed."
    message = "Check the project connection and local credentials, or run 'nauro auth login' again."
    if isinstance(exc, jwt.PyJWTError):
        message = "The returned access token could not be verified."
    return message
