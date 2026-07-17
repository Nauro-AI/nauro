# Nauro Privacy & Data Paths

Last updated: 2026-07-17

## Cloud sync

Project context (decisions, state, and open questions, but not source code) is stored encrypted in AWS S3 (us-east-1, SSE-S3). Each user's data is isolated under a unique prefix derived from their authentication identity. There is no self-service deletion command at this time; contact support to request removal of your cloud data.

## Remote MCP

When connected to Claude AI, Perplexity, or another MCP client, your project context is read from S3 and delivered to the AI tool. The AI tool's own data handling policies apply to how it processes the response. Nauro does not control or monitor what the AI tool does with the context after delivery.

## Product analytics

Current Nauro releases do not send product analytics and do not include the PostHog SDK. No replacement product analytics provider is configured.

During Nauro 1.x, `nauro telemetry status`, `enable`, `disable`, and `reset` remain as deprecated compatibility commands. They make no network requests and do not read, create, or modify telemetry config. The command group and all four shims will be removed in Nauro 2.0.

Existing `telemetry` sections in `~/.nauro/config.json` are ignored and left untouched. Nauro does not migrate or automatically delete them.

### Historical PostHog data

Earlier local releases could send opt-in command, MCP tool, sync, and project-created events to PostHog. When a user was authenticated, analytics identity handling could associate events with the Auth0 user id and a hash of the normalized email address. Earlier hosted MCP deployments could also send tool-use events.

Historical events may remain in PostHog under the retention settings that applied when they were collected. Removing current event emission does not delete those events, revoke the ingestion key used by older releases, or delete the PostHog project.

Operational metrics on the Lambda backend (request latency, error counts, and throttles) go to AWS CloudWatch and never include user content.
