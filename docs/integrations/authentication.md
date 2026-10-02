# Local authentication and recovery

Run authentication commands from the affected project repository. Projects that
use generation storage select credentials for that project's trusted connection.

```sh
nauro auth login
nauro auth status
```

Login opens the default browser and prints a fallback URL. Keep the terminal
command running while completing login. If it times out, start a new login.

Generation credentials require explicit renewal when the access token expires:

```sh
nauro auth refresh
```

This renews credentials without replaying a project request or syncing data.
Authentication status checks and reads do not silently renew credentials.

| Result | Recovery |
| --- | --- |
| `active` | Continue using Nauro. |
| `expired` | Run `nauro auth refresh`. |
| `logged_out` | Run `nauro auth login`. |
| `reauthentication_required` | Run `nauro auth login`; renewal cannot reuse the saved credentials. |
| Token endpoint could not be reached before sending | Credentials remain saved. Check the connection, then retry renewal explicitly. |
| Exchange outcome is uncertain or token verification fails after exchange | Sign in again. The old rotating token may have been consumed. |
| Local storage or callback access denied | Check filesystem and sandbox permissions. |
| Callback port in use | Finish or close the other login attempt, then retry. |

Successful renewal supports both rotating and non-rotating tokens. If the server
returns a replacement refresh token, Nauro saves it. If a refresh response omits
that field, Nauro retains the existing token. Initial login must provide a refresh
token. Null, empty, and invalid replacement values are rejected.

Nauro makes no automatic exchange retry. Rate-limit responses, server errors,
lost responses, and interrupted renewal still require login when consumption of
the prior token cannot be ruled out. A failed signing-key fetch after the exchange
also requires login. Only connection establishment or pool acquisition failures
at the token endpoint permit restoration of the prior credentials.

For an explicit reference profile, add `--reference-profile <path>` to each
authentication command. Use the same profile for login, status, refresh, and logout.

Errors report safe categories. Do not include tokens, provider response bodies,
authorization codes, or full authorization URLs in bug reports.
