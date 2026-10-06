import httpx
import jwt
import pytest

from nauro.sync.auth_errors import (
    AuthenticationError,
    RenewalRequiredError,
    VerificationRequiredError,
    auth_error_message,
)


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        (
            AuthenticationError("rate_limited"),
            "The authentication service rate limit was reached.",
        ),
        (
            AuthenticationError("service_unavailable"),
            "The authentication service is unavailable.",
        ),
        (ValueError("synthetic secret"), "Authentication failed."),
        (httpx.ReadTimeout("synthetic secret"), "The authentication request timed out."),
    ],
)
def test_fenced_renewal_gives_only_login_recovery(cause, reason):
    assert auth_error_message(RenewalRequiredError(cause)) == (
        f"{reason} Renewal incomplete; login required. Run 'nauro auth login' again."
    )


def test_unfenced_rate_limit_keeps_retry_guidance():
    assert auth_error_message(AuthenticationError("rate_limited")) == (
        "The authentication service rate limit was reached. Wait before trying again."
    )


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (jwt.ExpiredSignatureError, "The returned access token has expired."),
        (jwt.ImmatureSignatureError, "The token timestamp is ahead of this machine's clock."),
        (jwt.InvalidSignatureError, "The access token signature is invalid."),
        (jwt.InvalidIssuerError, "The access token issuer does not match this connection."),
        (jwt.InvalidAudienceError, "The access token audience does not match this connection."),
        (jwt.MissingRequiredClaimError, "The access token is missing a required claim."),
        (jwt.InvalidIssuedAtError, "The access token has an invalid issue timestamp."),
        (jwt.DecodeError, "The access token format or claim types are invalid."),
    ],
)
def test_jwt_errors_are_specific_without_exposing_exception_contents(cause, expected):
    assert auth_error_message(cause("synthetic secret")) == expected


def test_expired_pending_token_leads_with_login_recovery():
    assert auth_error_message(VerificationRequiredError(jwt.ExpiredSignatureError("secret"))) == (
        "The returned access token has expired. Access remains blocked. "
        "Run 'nauro auth login'. "
        "If this machine's clock is incorrect, correct it before signing in."
    )
