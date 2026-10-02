import httpx
import pytest

from nauro.sync.auth_errors import AuthenticationError, RenewalRequiredError, auth_error_message


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
