# Cross-surface parity tests

These are Layer-3 cross-surface parity tests. Each one runs the same kernel
operation against two stores and asserts the results are identical:

- `nauro.store.filesystem_store.FilesystemStore` — the local store that ships
  in this package.
- `mcp_server.store.cloud_store.CloudStore` — the S3-backed store that ships in
  the separate, private mcp-server repository.

The guarantee they encode is that the local and cloud surfaces return the same
result envelope for the same inputs, so the two implementations cannot drift
apart unnoticed.

## Why they are not in the CI gate

These tests require both the `nauro` CLI and the private `mcp_server` package to
be importable on a single `PYTHONPATH`. No CI environment has both stores
installed at once: this public monorepo never installs `mcp_server`, and the
mcp-server repo does not vendor the `nauro` test suite. Running them in CI would
only ever produce skips, which is a guarantee that never actually runs. They are
therefore excluded from the gate (`--ignore=packages/nauro/tests/cross_surface`
on the `test-nauro` job) and kept here as an opt-in suite for engineers who have
both repositories checked out.

Each test calls `pytest.importorskip("mcp_server.store.cloud_store", ...)` at
module load. When `mcp_server` is not installed, the test skips with a clear
reason instead of erroring, so running this directory without the cloud store
present is harmless.

## Running them locally

You need both packages importable on one `PYTHONPATH` (the nauro workspace plus
the private mcp-server repo). With that in place, run:

```bash
uv run --package nauro pytest packages/nauro/tests/cross_surface/ -v
```

If `mcp_server` (or its `boto3`/`moto` test dependencies) is not installed, the
tests skip with the CloudStore reason rather than failing.
