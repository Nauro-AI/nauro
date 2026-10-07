# Local authentication and recovery

Run authentication commands from the affected project repository. Projects that
use generation storage select credentials for that project's trusted connection.

```sh
nauro auth login
nauro auth status
```

Login opens the default browser and prints a fallback URL. Keep the terminal
command running while completing login. If it times out, start a new login.

CLI and MCP operations on installed generation projects renew credentials before
sending authenticated work when the access token has expired or has at most
60 seconds remaining. This also applies to later calls in the same MCP session.
Concurrent processes share the stored result instead of each exchanging a token.
Authentication status checks do not renew credentials.

Automatic credential acquisition has an eight-second budget, including waiting
for another refresher. Network renewal runs in a supervised process that is
stopped and reaped on timeout; it stops its own network work before that so an
unreachable endpoint leaves the saved credentials in place. A request that was
sent but never answered is an uncertain exchange and needs a new login, as with
explicit renewal. A failed attempt pauses further automatic attempts for up to
30 seconds. While the current access token is still valid, an attempt that did
not reach the endpoint does not block the operation. Explicit renewal remains
available for recovery:

```sh
nauro auth refresh
```

This renews credentials without replaying a project request or syncing data.
Credential renewal does not replay a request or recover stale replica contents.

| Result | Recovery |
| --- | --- |
| `active` | Continue using Nauro. |
| `expired` | The next authenticated operation attempts renewal. Use `nauro auth refresh` for explicit recovery. |
| `logged_out` | Run `nauro auth login`. |
| `verification_required` | Automatic acquisition or `nauro auth refresh` verifies the saved response without another exchange. If the saved token has expired, run `nauro auth login`. Access remains blocked until verification succeeds. |
| `reauthentication_required` | Run `nauro auth login`; renewal cannot reuse the saved credentials. |
| Token endpoint could not be reached before sending | Credentials remain saved. Check the connection, then retry renewal explicitly. |
| Exchange outcome is uncertain | Sign in again. The old rotating token may have been consumed. |
| Token verification fails after a complete response is saved | Correct the reported clock, connection, or token configuration problem, then retry verification with `nauro auth refresh`. Sign in again when the error asks for login or verification cannot succeed. |
| Local storage or callback access denied | Check filesystem and sandbox permissions. |
| Callback port in use | Finish or close the other login attempt, then retry. |

Successful renewal supports both rotating and non-rotating tokens. If the server
returns a replacement refresh token, Nauro saves it. If a refresh response omits
that field, Nauro retains the existing token. Initial login must provide a refresh
token. Null, empty, and invalid replacement values are rejected.

Nauro makes no automatic exchange retry. Rate-limit responses, server errors,
lost responses, and renewal interrupted before the response is saved still require
login when consumption of the prior token cannot be ruled out. Only connection
establishment or pool acquisition failures at the token endpoint permit restoration
of the prior credentials.

A complete refresh response is saved in owner-only credential storage before
verification. It remains blocked from use, including after a process restart.
Automatic acquisition or an explicit refresh retries verification, including signing-key
retrieval, without reusing the old refresh token or submitting another exchange.
Logout deletes the saved response. If the saved access token expires before it can
be verified, sign in again.

Token issue and not-before timestamps allow at most five seconds of clock skew.
This allowance does not extend token expiration or relax signature, issuer,
audience, account, client, or permission checks. Larger clock differences produce
a specific error. Check the operating system's time synchronization before retrying.
This allowance applies only to local verification. The server checks tokens against
its own clock and can refuse a token whose issue or not-before time is still in
the future there. Local `active` status does not guarantee server authorization.

Older clients report a saved, unverified response as `reauthentication_required`.
Use the updated client to resume verification. Credential files remain compatible;
older clients continue to block access to unverified tokens.

Explicit reference profiles keep manual renewal. Add `--reference-profile <path>` to each
authentication command. Use the same profile for login, status, refresh, and logout.

Errors report safe categories. A verification error can name the missing required
claim or the token library's error type, but never token contents or claim values.
Do not include tokens, provider response bodies, authorization codes, or full
authorization URLs in bug reports.
