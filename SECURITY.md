# Security Policy

## Reporting

Report vulnerabilities privately — not in a public issue:

- [GitHub's private reporting form](https://github.com/Nauro-AI/nauro/security/advisories/new) (preferred)
- email thomas@nauro.ai

You'll get an acknowledgment within a week. Nauro is maintained by one
person; confirmed issues in token handling or the hosted service are
treated as drop-everything work.

## Scope

The `nauro` and `nauro-core` PyPI packages, this repository, and the
hosted service (`mcp.nauro.ai` and the sync API). Nauro stores OAuth
tokens in `~/.nauro/config.json` — anything exposing those tokens, or
letting one user's cloud store be read or written by another, is high
severity.

**Out of scope:** `check_decision` being advisory rather than blocking
(by design); your own store's content influencing your agent (it's
your trusted input — cross-tenant injection via the hosted service is
in scope); DoS against the hosted endpoint; scanner reports without a
proof of concept.

## Disclosure

Fixes land on the latest 1.x release. Please allow reasonable time to
ship a fix before publishing details. Good-faith research under this
policy will never result in legal action; reporters are credited
unless they prefer otherwise.
