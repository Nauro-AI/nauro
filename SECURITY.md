# Security Policy

## Supported versions

Nauro is pre-1.0. Security fixes land on the latest published release of `nauro` and `nauro-core` on PyPI. Please upgrade to the latest version before reporting.

## Reporting a vulnerability

Please report security issues privately. Do not open a public issue for a suspected vulnerability.

- Preferred: use GitHub's private vulnerability reporting — open the **Security** tab of this repository and click **Report a vulnerability**.
- Or email **thomas@nauro.ai** with the details and, if possible, a proof of concept.

We aim to acknowledge a report within 3 business days and to keep you updated as we investigate. Once a fix is available we will coordinate disclosure and credit you if you would like.

## Scope

The `nauro` CLI and `nauro-core` library in this repository are open source (Apache 2.0), so their behavior can be reviewed here. The hosted sync service (`mcp.nauro.ai`) is operated separately and is not part of this repository. Reports about either are welcome through the channels above.

Issues we especially care about: leakage or mishandling of credentials and tokens, unauthorized access to a project's stored decisions, path traversal or arbitrary file read/write through the CLI or MCP server, and injection through stored decision content.
