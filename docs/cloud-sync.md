# Cloud sync

## What it does

`nauro sync` always captures a local snapshot of the project store at
`~/.nauro/projects/<project-id>/`. When cloud sync is configured, the CLI
also uploads each file to a managed S3 bucket so MCP clients (Claude.ai,
Perplexity, Cursor, and other MCP-aware tools) can read project context
server-side.

Without cloud sync, the snapshot exists only on the local machine and is
not reachable from a hosted client.

## Prerequisites today

Cloud sync currently requires AWS credentials that a Nauro administrator
provisions during onboarding:

- An S3 bucket scoped to your tenant.
- An IAM access key pair with read/write access to the user prefix inside
  that bucket.

Reach out to a Nauro administrator before configuring; the wizard cannot
mint these for you yet.

## Configuration

Run the interactive setup wizard once per machine:

```
nauro sync --cloud-setup
```

The wizard validates the credentials against the bucket and writes the
four sync keys into `~/.nauro/config.json` under the `sync` section:

- `sync.bucket_name`
- `sync.region`
- `sync.access_key_id`
- `sync.secret_access_key`

Environment variables of the same names (prefixed with `NAURO_SYNC_`)
override the file values, which is the recommended pattern for CI.

## Enabling cloud mode on a project

A project is created in local-only mode by `nauro init`. To promote it so
that subsequent syncs upload to S3:

```
nauro link --cloud <name>
```

`link --cloud` mints a server-side project id, re-keys the local store
under that id, and flips the project entry to `cloud` mode. It refuses
to run when sync credentials are not configured.

## Verifying

```
nauro sync --status
```

prints the current bucket, region, poll interval, and pending local or
remote changes for the resolved project. Use it after `--cloud-setup` to
confirm credentials are in place before linking a project.

## What's coming

A future release will move credential provisioning server-side. The CLI
will obtain short-lived upload URLs from the Nauro server rather than
holding long-lived AWS keys, and the `--cloud-setup` wizard will no
longer be required. Existing installs will continue to work during the
transition.
