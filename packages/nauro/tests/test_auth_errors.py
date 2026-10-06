import httpx
import jwt
import pytest

from nauro.sync.auth_errors import (
    AuthenticationError,
    InvalidExpiryClaimError,
    RenewalRequiredError,
    VerificationRequiredError,
    auth_error_message,
)

REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "azp", "scope")


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


@pytest.mark.parametrize("cause", [jwt.PyJWKError, jwt.InvalidKeyError, jwt.InvalidAlgorithmError])
def test_unclassified_jwt_error_names_its_type(cause):
    assert auth_error_message(cause("synthetic secret")) == (
        f"The returned access token could not be verified ({cause.__name__})."
    )


@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_missing_required_claim_is_named(claim):
    assert auth_error_message(jwt.MissingRequiredClaimError(claim)) == (
        f"The access token is missing the required claim '{claim}'."
    )


@pytest.mark.parametrize("claim", ["jti", "nbf", "synthetic secret"])
def test_unrequired_missing_claim_is_not_echoed(claim):
    assert auth_error_message(jwt.MissingRequiredClaimError(claim)) == (
        "The access token is missing a required claim."
    )


@pytest.mark.parametrize(
    "kind",
    [
        ValueError,
        jwt.PyJWTError,
        jwt.PyJWKError,
        jwt.InvalidKeyError,
        jwt.InvalidAlgorithmError,
        jwt.InvalidTokenError,
        jwt.DecodeError,
        jwt.MissingRequiredClaimError,
    ],
)
@pytest.mark.parametrize("wrap", [None, VerificationRequiredError, RenewalRequiredError])
@pytest.mark.parametrize("recovery", [True, False])
def test_exception_contents_never_appear(kind, wrap, recovery):
    cause = kind("synthetic secret")
    message = auth_error_message(wrap(cause) if wrap else cause, recovery=recovery)
    assert "synthetic secret" not in message


def test_signing_key_error_has_a_typed_message():
    assert auth_error_message(AuthenticationError("signing_key")) == (
        "The access token signing key does not match a supported issuer key. "
        "Check the OAuth issuer settings."
    )


def test_invalid_expiry_claim_leads_with_login_recovery():
    assert auth_error_message(InvalidExpiryClaimError()) == (
        "The access token expiry claim is not an integer timestamp. Run 'nauro auth login' again."
    )
    assert auth_error_message(VerificationRequiredError(InvalidExpiryClaimError())) == (
        "The access token expiry claim is not an integer timestamp. Access remains blocked. "
        "Run 'nauro auth login' again."
    )
