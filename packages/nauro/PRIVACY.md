# Nauro Privacy & Data Paths

Last updated: 2026-06-05

## Cloud sync

Project context (decisions, state, open questions — not source code) is stored encrypted in AWS S3 (us-east-1, SSE-S3). Each user's data is isolated under a unique prefix derived from their authentication identity. There is no self-service deletion command at this time; contact support to request removal of your cloud data.

## Remote MCP

When connected to Claude AI, Perplexity, ChatGPT, or another MCP client, your project context is read from S3 and delivered to the AI tool. The AI tool's own data handling policies apply to how it processes the response. Nauro does not control or monitor what the AI tool does with the context after delivery.

## Telemetry

Nauro collects anonymous product-usage telemetry to understand which commands are used, where users get stuck, and whether the tool is healthy. Telemetry is **default opt-in** via a one-line first-run prompt that points back to this document.

### Events

Only the following events fire, with only the listed properties. No content, no identifiers beyond an anonymous per-machine UUID:

```
cli.command_invoked   { command, success, duration_bucket, nauro_version, os }
mcp.tool_called       { tool_name, transport, success, duration_bucket }
sync.completed        { snapshot_count, duration_bucket, bytes_bucket }
project.created       { schema_version }
```

### Never sent

- Decision titles
- Decision rationale
- Decision content
- File paths
- Repo names
- Project names
- MCP tool arguments
- MCP tool return values
- Stack traces
- Command-line arguments
- IP address
- Geolocation (country / region / city)

### Opting out

- `NAURO_TELEMETRY=0` environment variable — suppresses all telemetry and the first-run prompt.
- `nauro telemetry disable` — persists the opt-out in `~/.nauro/config.json`.

### Vendor

Product analytics events go to [PostHog](https://posthog.com) (cloud). PostHog is dual-licensed and self-hostable, which we mention as a credibility signal — Nauro itself does not currently support pointing at a self-hosted PostHog instance.

Operational metrics on the Lambda backend (request latency, error counts, throttles) go to AWS CloudWatch and never include user content.
