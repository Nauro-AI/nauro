# Nauro Privacy & Data Paths

Last updated: 2026-03-30

## Local extraction (free tier, BYOK)

Your code diffs go directly from your machine to your configured LLM provider (Anthropic API). Nauro's servers are never in the data path. The API key is stored locally at `~/.nauro/config.json` with owner-only file permissions (0o600).

## Hosted extraction (future Pro tier)

Code diffs transit a Nauro Lambda function and are sent to Anthropic's API for extraction. Diffs are not stored. Anthropic's data retention policies apply to API calls.

## Cloud sync

Project context (decisions, state, open questions — not source code) is stored encrypted in AWS S3 (us-east-1, SSE-S3). Each user's data is isolated under a unique prefix derived from their authentication identity. You can delete all your data at any time with `nauro account delete`.

## Remote MCP

When connected to Claude AI, Perplexity, ChatGPT, or another MCP client, your project context is read from S3 and delivered to the AI tool. The AI tool's own data handling policies apply to how it processes the response. Nauro does not control or monitor what the AI tool does with the context after delivery.
